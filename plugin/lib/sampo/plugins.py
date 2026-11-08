"""fm plugins: find a capability in the known marketplaces, check enabled plugins for conflicts with Sampo and with
each other, and install one after the user's yes (guard category `plugin`, granted only through `fm ask`).

Everything is read from disk (~/.claude/plugins): marketplace indexes, their local plugin copies and the install
cache. Always-on cost is estimated from skill/agent/command descriptions (~4 characters a token).
"""
import glob
import hashlib
import json
import os
import re
import subprocess

from sampo import core as c

# What Foreman already owns, and the skill/command names that duplicate it (process, not domain, knowledge).
FOREMAN_OWNS = {
    "brainstorming": ("brainstorm", "ideation"),
    "planning and intake": ("writing-plans", "executing-plans", "plan-mode", "planner", "feature-dev", "prd"),
    "verification gates": ("verification-before-completion", "verify-before", "quality-gate"),
    "task tracking and drive": ("todo", "task-tracker", "ralph"),
}
# Measured conflicts, by plugin name; setup-plugins.sh warns about the same ones (test_docs keeps the lists equal).
KNOWN_CONFLICTS = {
    "ecc": "~41k always-on tokens and 24 hook handlers, with its own memory, learning and planning; Sampo ports "
           "its useful procedures as /sampo:playbooks",
    "superpowers": "a second orchestrator (brainstorm → plan → execute); Sampo ports its debugging, TDD and "
                   "verification procedures",
    "feature-dev": "duplicates Sampo's intake and planning",
    "ralph-loop": "keeps Claude running via a Stop hook; collides with Sampo's completion gate",
    "example-skills": "12 mostly unrelated skills; duplicates skill-creator and frontend-design",
}
_STOP = {"a", "an", "the", "for", "and", "or", "of", "to", "with", "in", "on", "my", "i", "need", "plugin"}


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):  # RecursionError: "[" * 100000
        return default


def _frontmatter(path):
    if not os.path.isfile(path):  # a FIFO would block the open forever, a device never end
        return {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(4000)
    except OSError:
        return {}
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out = {}
    # `key: value`, or a block scalar (`key: >` / `key: |`) whose value is the indented lines that follow
    for key, value, block in re.findall(r"^(name|description):[ \t]*(.*)\n?((?:[ \t]+.*\n?)*)", m.group(1), re.M):
        value = value.strip()
        out[key] = " ".join(l.strip() for l in block.splitlines()) if value[:1] in (">", "|") else value
    return out


# T-1396: the Agent Skills spec's frontmatter rules, shared by fm doctor and fm lib's source indexer
SPEC_NAME = c.lazy(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# a description that claims every task (or tells the model to drop its other instructions) would win every routing
# choice: such a skill is indexed but held out of fm lib search. Matched on the NFKC-folded text with the common
# Cyrillic and Greek look-alikes mapped to Latin. ponytail: phrase list, not a model judgement; a reworded claim
# still passes (an LLM or embedding check if one gets through in practice).
BROAD = c.lazy(
    r"(?i)\b(?:(?:for|on|in|before|with|to|during|after)\s+(?:each\s+and\s+every|every|all|any|each)(?:\s+of)?"
    r"(?:\s+(?:the|your|my|their|the\s+user's|user's))?\s+(?:single\s+|other\s+)?"
    r"(?:tasks?|requests?|prompts?|messages?|conversations?|questions?|turns?|queries|query|responses?|interactions?)\b"
    r"|always\s+(?:use|invoke|load|activate|run)\s+(?:this|me)\b(?!\s+(?:skill\s+|agent\s+)?(?:when|if|for|to)\b)"
    r"|always\s+(?:use|invoke|load|activate|run)\s+(?:this|me)(?:\s+(?:skill|agent))?\s+(?:for|on|with|in)\s+"
    r"(?:anything|everything)\b"
    r"|before\s+(?:anything|everything)\b|for\s+everything\b|no\s+matter\s+what\b|regardless\s+of\s+the\s+task\b"
    r"|ignore\s+(?:all\s+|any\s+)?(?:previous|prior|other|earlier|the)\s+(?:instructions|skills|rules|agents)\b"
    r"|(?:replaces?|overrides?|supersedes?)\s+(?:all\s+|any\s+|every\s+)?(?:other\s+)?(?:skills|instructions|agents|rules)\b)"
    r"|(?:^|(?<=[.!?:;]\s))\s*(?:system|assistant|developer)(?:\s+(?:prompt|message|note))?\s*:"
    r"|<\s*/?\s*system\b")
_LOOKALIKES = str.maketrans("аеорсухіјѕԁԛԝАВЕКМНОРСТХІЈЅαεικνορτυχΑΒΕΗΙΚΜΝΟΡΤΥΧ’",
                            "aeopcyxijsdqwABEKMHOPCTXIJSaeikvoptuxABEHIKMNOPTYX'")


def _fold(s):
    import unicodedata
    return unicodedata.normalize("NFKC", s).translate(_LOOKALIKES)


def _unquote(v):
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v


def spec_problems(fm, folder=None, kind="skill"):
    """What a SKILL.md's (or an agent's) frontmatter, as _frontmatter read it, breaks of the Agent Skills spec, plus
    hidden characters and an over-broad description: [] when nothing. A skill's name must equal its folder; an agent
    (folder None) may leave its name to its file's. Every message is safe to print."""
    if not fm:
        return ["no frontmatter in its first 4,000 characters"]
    name, desc = _unquote(fm.get("name", "")), _unquote(fm.get("description", ""))
    out = ["control or hidden characters in its frontmatter"] if c.plain(name + desc) != name + desc else []
    if not name:
        out += ["no name in its frontmatter"] if kind == "skill" else []
    elif len(name) > 64:
        out.append(f"name over 64 characters ({len(name)})")
    elif not SPEC_NAME.fullmatch(name):
        out.append(f"name {c.plain(name)[:40]!r} breaks the spec (lowercase letters, digits and single hyphens)")
    elif folder is not None and name != folder:
        out.append(f"name {name!r} doesn't match its folder {c.plain(folder)[:40]!r}")
    if not desc:
        out.append("no description")
    elif len(desc) > 1024 and kind == "skill":  # the skills spec's cap: agents' <example> blocks run longer
        out.append(f"description over 1,024 characters ({len(desc)})")
    broad = BROAD.search(_fold(desc))
    if broad:
        out.append(f"over-broad description ({c.plain(broad.group(0))!r}): held out of search")
    return out


def profile(path):
    """What a plugin directory brings: skills, commands, agents (name, description), hook events, MCP servers and an
    always-on token estimate."""
    out = {"skills": [], "commands": [], "agents": [], "hooks": set(), "mcp": {}, "tokens": 0, "sync_stop": False}
    if not path or not os.path.isdir(path):
        return out
    seen = set()  # T-1444: the default folders and plugin.json's paths (nested commands too), confined to the plugin
    for kind, f, _ in _component_files(path):
        real = os.path.realpath(f) if f else None
        # a default folder may link out (as before); a named path is already confined by _component_files
        if real is None or real in seen:
            continue
        seen.add(real)
        kind += "s"
        fm = _frontmatter(f)
        # unnamed: a skill is its folder's name, a command or agent its file's name without .md
        name = fm.get("name") or (os.path.basename(os.path.dirname(f)) if kind == "skills" else
                                  os.path.basename(f)[:-3])
        out[kind].append((name, fm.get("description", "")))
        out["tokens"] += (len(name) + len(fm.get("description", ""))) // 4 + 5
    hooks, out["mcp"] = _plugin_spec(path, "hooks"), _plugin_spec(path, "mcpServers")
    hooks.pop("modules", None)  # T-1434: a hooks module list, not an event
    out["hooks"] = set(hooks)
    # async/asyncRewake Stop hooks run after the turn ends and can't hold it open
    out["sync_stop"] = any(not (h.get("async") or h.get("asyncRewake")) for e, _, h in _handlers(hooks) if e == "Stop")
    return out


_SPEC_CAP = 8_000_000  # over the supply hash's 4 MB per file, so a padded hooks file is still read (T-1392 review)


def _plugin_spec(path, key, unread=None):
    """A plugin's hooks, mcpServers or lspServers as one dict: the default file (hooks/hooks.json, .mcp.json,
    .lsp.json) plus whatever plugin.json names (inline, a path or a list of paths). Both are read, the safe superset
    whichever way Claude Code merges them (T-1392 review); hook events are concatenated, and a hooks file's module
    list comes back as "modules" (T-1434: a mod's {"modules": [...]}). A named file outside the plugin root, not a
    regular file or over 8 MB is skipped and its path appended to unread; malformed entries are skipped."""
    manifest = _spec_file(path, ".claude-plugin/plugin.json", unread)
    extra = manifest.get(key) if isinstance(manifest, dict) else None
    out = {}
    for spec in [{"hooks": "hooks/hooks.json", "mcpServers": ".mcp.json", "lspServers": ".lsp.json"}[key]] + (
            extra[:50] if isinstance(extra, list) else [extra]):  # ponytail: a manifest's first 50 paths
        raw = _spec_file(path, spec, unread) if isinstance(spec, str) else spec
        data = raw.get(key, raw) if isinstance(raw, dict) else None
        if key == "hooks" and isinstance(raw, dict) and data is not raw and "modules" in raw:
            data = dict(data if isinstance(data, dict) else {}, modules=raw["modules"])  # {"hooks", "modules"}: both run
        for k, v in (data.items() if isinstance(data, dict) else ()):
            if isinstance(v, list):
                out[k] = (out[k] if isinstance(out.get(k), list) else []) + v
            else:
                out.setdefault(k, v)
    return out


def _spec_file(root, rel, unread, cap=_SPEC_CAP, malformed=None):
    """The JSON at rel under a plugin root, confined to it and capped in size (None when absent or malformed; rel is
    appended to malformed, when given, for JSON that doesn't parse)."""
    given = os.path.join(root, rel)
    if not os.path.lexists(given):
        return None
    full, base = os.path.realpath(given), os.path.realpath(root)
    try:
        ok = full.startswith(base + os.sep) and os.path.isfile(full) and os.path.getsize(full) <= cap
        if ok:  # isfile first: a FIFO or device would block or never end
            with open(full, "rb") as f:
                raw = f.read(cap + 1)
            ok = len(raw) <= cap
    except OSError:
        ok = False
    if not ok:
        if unread is not None:
            unread.append(rel)
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        if malformed is not None:
            malformed.append(rel)
        return None


REPO_CONFIG = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json")
_REPO_CAP = 256_000
_SETTING_COMMANDS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper")  # each runs
_NOTE_CAP = 1500


def repo_config(root):
    """T-1472: what a repo's own Claude config runs (hook commands, the status line, MCP servers, allow rules) as
    (content hash, one note), or (None, None) when it has none of REPO_CONFIG. Reads go through _spec_file: a file
    over 256 KB, not regular or resolving outside the repo is named, never read; malformed JSON is named."""
    h, unread, bad, settings, hooks, mcp, allow = hashlib.sha256(), [], [], [], [], [], []
    present = [rel for rel in REPO_CONFIG if os.path.lexists(os.path.join(root, rel))]
    for rel in present:
        n = len(unread) + len(bad)
        data = _spec_file(root, rel, unread, cap=_REPO_CAP, malformed=bad)
        if len(unread) + len(bad) > n:  # unread: its target, size and time stand in for its content
            try:
                st = os.stat(os.path.join(root, rel))
                stamp = [os.path.realpath(os.path.join(root, rel)), st.st_size, st.st_mtime_ns]
            except OSError:
                stamp = ["gone"]
        else:
            stamp = data
        h.update(json.dumps([rel, stamp], sort_keys=True, default=str).encode() + b"\0")
        if not isinstance(data, dict):
            continue
        if rel == ".mcp.json":
            servers = data.get("mcpServers")
            for name, spec in (servers.items() if isinstance(servers, dict) else ()):
                spec = spec if isinstance(spec, dict) else {}
                run = spec.get("url") or " ".join(str(x) for x in [spec.get("command") or ""] + (
                    spec.get("args")[:10] if isinstance(spec.get("args"), list) else []))
                mcp.append(f"{name}: {run}")
            continue
        events = data.get("hooks")
        hooks += [f"{e}{' ' + str(m) if m else ''}: {x.get('command') or x.get('url') or x.get('prompt') or '?'}"
                  for e, m, x in _handlers(events if isinstance(events, dict) else {})]
        for k in ("statusLine", "fileSuggestion"):
            if isinstance(data.get(k), dict) and data[k].get("command"):
                settings.append(f"{k}: {data[k]['command']}")
        settings += [f"{k}: {data[k]}" for k in _SETTING_COMMANDS if isinstance(data.get(k), str) and data[k]]
        if data.get("enableAllProjectMcpServers") is True:
            settings.append("enableAllProjectMcpServers: every .mcp.json server starts unasked")
        for k in ("env", "enabledPlugins", "extraKnownMarketplaces"):  # names only: an env value may be a secret
            if isinstance(data.get(k), dict) and data[k]:
                settings.append(f"{k}: " + ", ".join(map(str, data[k])))
        perms = data.get("permissions")
        rules = perms.get("allow") if isinstance(perms, dict) else None
        allow += [str(r) for r in (rules if isinstance(rules, list) else ())]
    if not present:
        return None, None

    def group(label, items, each=4):
        shown = "; ".join(c.fit(c.redact(c.plain(str(x))), 90) for x in items[:each])
        return f"{label} {len(items)} ({shown}{f'; and {len(items) - each} more' if len(items) > each else ''})"

    parts = []  # what a hostile repo can't pad away comes first: the note is cut from the end
    if unread:
        parts.append("not read (a symlink out of the repo, over 256 KB or not a regular file): " + ", ".join(unread))
    if bad:
        parts.append("malformed JSON: " + ", ".join(bad))
    parts += [group(label, items, each) for label, items, each in (
        ("settings", settings, 6), ("hook commands", hooks, 4), ("MCP servers", mcp, 4), ("allow rules", allow, 8))
        if items]
    note = ("This repo's own Claude config (" + ", ".join(present) + "; Claude Code may already have started these "
            "when the session opened, Sampo names them and can't stop them; review before trusting the repo): "
            + "; ".join(parts or ["nothing"]) + ".")
    return h.hexdigest()[:16], c.fit(c.defang(note), _NOTE_CAP)


def _handlers(hooks):
    """(event, matcher, handler dict) for every hook handler, skipping malformed entries."""
    for event, entries in hooks.items():
        for e in entries if isinstance(entries, list) else ():
            for h in (e.get("hooks") if isinstance(e, dict) and isinstance(e.get("hooks"), list) else ()):
                if isinstance(h, dict):
                    yield event, e.get("matcher") or "", h


_ROOT_PATH = c.lazy(r"\$\{?CLAUDE_PLUGIN_ROOT\}?/([\w./@+~-]+)")  # T-1459 review: @scope/pkg+x paths whole
_FM_CAP = 256_000  # a frontmatter not closed by here is the event "?" (ponytail: Claude Code reads the whole file)
_FM_FILES, _FM_DIRS = 2000, 5000  # past these a component folder is the event "?" (a hostile tree stays bounded)
# a top-level hooks key, quoted or spaced (review), and its block: indented lines, blank lines, column-0 comments
_FM_BLOCK = c.lazy(r"""^["']?hooks["']?[ \t]*:[ \t]*(.*)\n?((?:[ \t]+.*\n?|[ \t\r]*\n|#.*\n?)*)""", re.M)
# anything else that may be a hooks key (a flow-style frontmatter, an indented root, a "? hooks" key): unknown
_FM_MENTION = c.lazy(r"""\bhooks\b["']?[ \t]*:|\?[ \t]*["']?hooks\b""")


def _component_files(root):
    """T-1434 review: (kind, path or None, label) of each skill, command and agent file Claude Code may read hooks
    from: the default folders and the paths plugin.json's skills, commands and agents name, folders walked whole
    (nested command folders too). A named path that isn't a string, lies outside the plugin root or holds more than
    the caps comes back with path None: unknown, never "no hooks"."""
    manifest = _spec_file(root, ".claude-plugin/plugin.json", None)
    base = os.path.realpath(root)
    out, dirs_seen = [], set()
    for kind, key in (("skill", "skills"), ("command", "commands"), ("agent", "agents")):
        extra = manifest.get(key) if isinstance(manifest, dict) else None
        for i, rel in enumerate([key] + (extra[:50] if isinstance(extra, list) else [] if extra is None else [extra])):
            label = c.plain(str(rel))[:60]
            full = os.path.realpath(os.path.join(root, rel)) if isinstance(rel, str) and "\0" not in rel else None
            # the default folder is followed wherever it links (as before); a named path must stay in the root
            if full is None or i and full != base and not full.startswith(base + os.sep):
                out.append((kind, None, label))
                continue
            if os.path.isfile(full):
                out.append((kind, full, label))
                continue
            # a folder it can't list is unknown; one that isn't there (no agents/) is nothing
            unlisted = lambda e, kind=kind, label=label: os.path.lexists(e.filename or "") and out.append(
                (kind, None, label))
            for d, subs, files in os.walk(full, onerror=unlisted, followlinks=True):
                real = (kind, os.path.realpath(d))
                if real in dirs_seen:  # a link loop, or a folder two paths name, is walked once
                    subs[:] = []
                    continue
                dirs_seen.add(real)
                subs.sort()
                out += [(kind, os.path.join(d, f), label) for f in sorted(files)
                        if (f == "SKILL.md" if kind == "skill" else f.endswith(".md"))]
                if len(dirs_seen) > _FM_DIRS or len(out) > _FM_FILES:
                    del out[_FM_FILES:]
                    out.append((kind, None, label))
                    break
    return out


def _frontmatter_hooks(root):
    """T-1434: hooks declared in skill, agent and command frontmatter: (["Event (skill name)", …], ["skill name
    digest", …], the blocks' text, for the scripts they name). A block's events are its least-indented keys, or a
    flow mapping's top-level keys. Review: anything that may declare hooks but can't be read for sure (no events
    found, a frontmatter not closed in the read window, a hooks key outside a block, a path it won't walk) is the
    event "?", still held when it appears."""
    events, specs, text = set(), set(), []
    for kind, f, label in _component_files(root):
        if f is None:
            events.add(f"? ({kind} {label})")
            continue
        try:
            if not os.path.isfile(f):  # a FIFO would block the open, a device never end
                raise OSError
            with open(f, encoding="utf-8", errors="replace") as fh:
                raw = fh.read(_FM_CAP).lstrip("﻿")
        except OSError:  # it may hold hooks Foreman can't see
            events.add(f"? ({kind} {c.plain(os.path.relpath(f, root))[:60]})")
            continue
        m = re.match(r"---[ \t]*\r?\n(.*?)^---", raw, re.S | re.M)
        # less the newline before the close, as T-1434 first read it, so recorded digests still match
        fm = re.sub(r"\r?\n\Z", "", m.group(1)) if m else raw if re.match(r"---[ \t]*\r?\n", raw) else ""
        name = re.search(r"""^["']?name["']?[ \t]*:[ \t]*(.+)$""", fm, re.M)
        name = _unquote(name.group(1).strip()) if name else (
            os.path.basename(os.path.dirname(f)) if kind == "skill" else os.path.basename(f)[:-3])
        where = f"{kind} {c.plain(str(name))[:60]}"
        if fm and not m:  # opened but not closed in the window: whatever follows may be hooks
            events.add(f"? ({where})")
            continue
        blocks = list(_FM_BLOCK.finditer(fm))
        rest = "".join(fm[a:b] for a, b in zip([0] + [x.end() for x in blocks], [x.start() for x in blocks] + [len(fm)]))
        if _FM_MENTION.search(rest):
            events.add(f"? ({where})")
        for block in blocks:
            inline, body = block.group(1).strip(), block.group(2)
            inline = "" if inline.startswith("#") else inline  # "hooks:  # comment" then a block
            if inline:  # flow style: {Stop: [...], PreToolUse: [...]}, keys at depth one, over as many lines as it takes
                keys, depth, tok = [], 0, ""
                for ch in inline + "\n" + body:
                    depth += (ch in "{[") - (ch in "}]")
                    tok = "" if ch in "{[,}]" else tok + ch
                    if ch == ":" and depth == 1:
                        keys.append(_unquote(tok[:-1].strip()))
            else:
                lines = [l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("#")]
                indent = min((len(l) - len(l.lstrip()) for l in lines), default=0)
                keys = [_unquote(l.split(":", 1)[0].strip()) for l in lines
                        if len(l) - len(l.lstrip()) == indent and ":" in l and not l.lstrip().startswith("-")]
            events.update(f"{k or '?'} ({where})" for k in keys or ["?"])
            specs.add(f"{where} {hashlib.sha256((inline + body).encode()).hexdigest()[:16]}")
            text.append(inline + body)
    return sorted(events), sorted(specs), "\n".join(text)


def capabilities(root, prof=None):
    """T-1392: what a plugin tree runs on its own (hook events, each hook handler as "event matcher: command", MCP
    server names and "name digest" of each server's spec, scripts: bin/ and the files its hooks and MCP servers name
    under ${CLAUDE_PLUGIN_ROOT}, and spec files it names but Sampo won't read) and what it adds to the session
    (command, agent and skill names), each a sorted list. fm doctor holds an update that adds to the first group.
    T-1434: also hooks in skill, agent and command frontmatter (fm_hooks "Event (skill s)", fm_hook_specs "skill s
    digest"), LSP servers (lsp, lsp_specs, like MCP) and a hooks file's modules (paths as written, normalised).
    ponytail: an existing handler's script edited in place is only the tree hash's note, and hooks inlined in a
    marketplace entry aren't read (they apply at install, where fm plugins find shows needs)."""
    prof = prof or profile(root)
    unread = []
    hooks, mcp, lsp = (_plugin_spec(root, k, unread) if root and os.path.isdir(root) else {}
                       for k in ("hooks", "mcpServers", "lspServers"))
    mods = hooks.pop("modules", None)
    # review: an entry that isn't a path (an object, a number) is the module "?", never dropped
    modules = sorted({os.path.normpath(m) if isinstance(m, str) and m else "?"
                      for m in (mods if isinstance(mods, list) else [] if mods is None else [mods])})
    fm_hooks, fm_specs, fm_text = _frontmatter_hooks(root) if root and os.path.isdir(root) else ([], [], "")
    # a ": " in a matcher would read as the separator and push the command out of fm doctor's window
    sep = lambda s: str(s).replace(": ", ":")
    handlers = {f"{sep(e)} {sep(m)}: {h.get('command') or h.get('url') or h.get('prompt') or ''}"
                for e, m, h in _handlers(hooks)}
    digest = lambda spec: hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]
    scripts = {os.path.normpath(p) for p in _ROOT_PATH.findall(json.dumps([hooks, mcp, lsp]) + fm_text)}
    bindir = os.path.join(root or "", "bin")
    for d, dirs, files in os.walk(bindir) if root and os.path.isdir(bindir) else ():
        dirs.sort()
        scripts.update(os.path.relpath(os.path.join(d, f), root) for f in files)
        if len(scripts) > 500:  # ponytail: a bin/ this big is reported by its first 500 files
            break
    names = lambda kind: sorted({n for n, _ in prof[kind] if isinstance(n, str)})
    return {"hooks": sorted(map(str, hooks)), "handlers": sorted(handlers), "mcp": sorted(map(str, mcp)),
            "mcp_specs": sorted(f"{k} {digest(v)}" for k, v in mcp.items()), "scripts": sorted(scripts),
            "unread": sorted(set(unread)), "commands": names("commands"), "agents": names("agents"),
            "skills": names("skills"), "fm_hooks": fm_hooks, "fm_hook_specs": fm_specs,
            "lsp": sorted(map(str, lsp)), "lsp_specs": sorted(f"{k} {digest(v)}" for k, v in lsp.items()),
            "modules": modules}


def _marketplace_files():
    """Each known marketplace's marketplace.json: the cached ones, and directory sources registered where they live
    (known_marketplaces.json installLocation, e.g. Sampo's own repo)."""
    files = glob.glob(os.path.join(claude_dir(), "plugins", "marketplaces", "*", ".claude-plugin", "marketplace.json"))
    known = _json(os.path.join(claude_dir(), "plugins", "known_marketplaces.json"), {}) or {}
    for entry in known.values() if isinstance(known, dict) else ():
        loc = entry.get("installLocation") if isinstance(entry, dict) else None
        if isinstance(loc, str):
            files.append(os.path.join(loc, ".claude-plugin", "marketplace.json"))
    seen, out = set(), []
    for f in files:
        real = os.path.realpath(f)
        if real not in seen and os.path.isfile(real):
            seen.add(real)
            out.append(f)
    return sorted(out)


_PLUGIN_NAME = c.lazy(r"[\w.-]+")  # the name part of fmguard.PLUGIN_ID: no @, no slash, no space


def _marketplace_name(data, root):
    """A marketplace's name: its own when it is a plugin name, else its folder's made into one (never a control
    character, an @ or a slash)."""
    name = data.get("name") if isinstance(data, dict) else None
    if isinstance(name, str) and _PLUGIN_NAME.fullmatch(name):
        return name
    return re.sub(r"[^\w.-]+", "-", os.path.basename(root), flags=re.A).strip("-") or "marketplace"


def index(skipped=None):
    """Every plugin in the known marketplaces: id, description, category, and its local copy when there is one;
    `renamed_from` the old names the marketplace's `renames` maps to it, `identity` its (marketplace, name, source)
    for trust(). T-1396: an entry that isn't an object, has no usable name, repeats an id or points its local source
    outside its marketplace is left out, its reason appended to skipped (a list) when given."""
    out, seen = [], set()
    skip = (lambda why: skipped.append(c.plain(why))) if skipped is not None else (lambda why: None)
    for f in _marketplace_files():
        root, data = os.path.dirname(os.path.dirname(f)), _json(f, {})
        if not isinstance(data, dict):
            skip(f"{os.path.basename(root)}: marketplace.json isn't a JSON object")
            continue
        mk = _marketplace_name(data, root)
        plugins = data.get("plugins")
        if not isinstance(plugins, list):
            if plugins is not None:
                skip(f"{mk}: plugins isn't a list")
            plugins = []
        renames = data.get("renames") if isinstance(data.get("renames"), dict) else {}
        renamed = {}
        for old, new in renames.items():
            if isinstance(new, str):
                renamed.setdefault(new, []).append(c.plain(old)[:60])
        real_root = os.path.realpath(root)

        def outside(local):
            try:
                real = os.path.realpath(local)
            except (OSError, ValueError):  # a NUL in the source
                return "bad source path"
            return "its source is outside the marketplace" if real != real_root and \
                not real.startswith(real_root + os.sep) else None
        for i, p in enumerate(plugins, 1):
            name = p.get("name") if isinstance(p, dict) else None
            src = p.get("source") if isinstance(p, dict) else None
            local = os.path.normpath(os.path.join(root, src)) if isinstance(src, str) else None
            why = ("not an object" if not isinstance(p, dict) else "no name" if not isinstance(name, str) or not name
                   else f"name {c.plain(name)[:40]!r} isn't a plugin name" if not _PLUGIN_NAME.fullmatch(name)
                   else f"a second entry named {name}" if f"{name}@{mk}" in seen
                   else outside(local) if local else None)
            if why:
                skip(f"{mk}: entry {i}: {why}")
                continue
            seen.add(f"{name}@{mk}")
            text = lambda k: c.plain(p[k]) if isinstance(p.get(k), str) else ""
            out.append({"id": f"{name}@{mk}", "name": name, "marketplace": mk,
                        "description": text("description"), "category": text("category"),
                        "local": local if local and os.path.isdir(local) else None,
                        "renamed_from": sorted(renamed.get(name, [])),
                        "identity": _digest(json.dumps([mk, name, src], sort_keys=True).encode()),
                        "entry_hash": _digest(json.dumps(p, sort_keys=True).encode())})
    return out


def _trust_path():
    return os.path.join(c.state_dir(), "plugin-trust.json")


def record_trust(pid, fresh=True):
    """Remember the identity (marketplace, name, source with its sha) of a plugin fm plugins installed or enabled. An
    enable (fresh False) records only a plugin the ledger doesn't know: it must not bless a source that changed since
    the install. ponytail: read-modify-write without a lock; a lost record only makes a plugin read as untrusted."""
    row = next((p for p in index() if p["id"] == pid), None)
    if not row:
        return
    ledger = _json(_trust_path(), {})
    ledger = ledger if isinstance(ledger, dict) else {}
    if not fresh and ledger.get(pid) not in (None, row["identity"]):
        return
    ledger[pid] = row["identity"]
    c.write_atomic(_trust_path(), json.dumps(ledger, indent=1, sort_keys=True) + "\n")


def trust(row, ledger=None):
    """(trusted, why) for an index() row (T-1396): trusted only when fm plugins installed this very identity; a plugin
    the marketplace renamed, or whose source or sha changed since, reads as a new, untrusted one (why says which).
    (False, "") for one fm plugins never installed."""
    ledger = _json(_trust_path(), {}) if ledger is None else ledger
    had = ledger.get(row["id"]) if isinstance(ledger, dict) else None
    if had == row["identity"]:
        return True, ""
    if row["renamed_from"]:
        return False, f"renamed from {', '.join(row['renamed_from'])}: a new plugin to Sampo, not the one you had"
    if had:
        return False, "its name, source or sha changed since fm plugins installed it"
    return False, ""


def _digest(data):
    return hashlib.sha256(data).hexdigest()[:16]


def _tree_hash(root):
    """Every file under root (paths and contents; symlinks as their targets, never followed; .git skipped)."""
    h = hashlib.sha256()
    for d, dirs, files in os.walk(root):
        links = sorted(x for x in dirs if os.path.islink(os.path.join(d, x)))
        dirs[:] = sorted(x for x in dirs if x != ".git" and x not in links)
        for f in sorted(files) + links:
            path = os.path.join(d, f)
            h.update(os.path.relpath(path, root).encode() + b"\0")
            if os.path.islink(path):
                h.update(b"link:" + os.readlink(path).encode())
            elif os.path.isfile(path):
                try:
                    with open(path, "rb") as fh:
                        h.update(hashlib.sha256(fh.read()).digest())
                except OSError:
                    h.update(b"unreadable")  # can't vouch for it: differs from any readable content
    return h.hexdigest()[:16]


def _pin_source(pid):
    """(the code a pin on pid hashes: its installed copy, else the marketplace's local copy, or None; its marketplace
    row, None for an installed copy or an unknown plugin)."""
    have = installed().get(pid) or {}
    if have.get("path") and os.path.isdir(have["path"]):
        return have["path"], None
    known = next((p for p in index() if p["id"] == pid), None)
    return (known or {}).get("local"), known


def content_hash(pid):
    """What a yes to install or enable pid approves (T-0036): its installed copy when there is one (enable), else its
    marketplace entry plus the marketplace's local copy of it, or the entry alone for a remote source (which pins the
    code only as far as the entry names a ref or sha). None for an unknown plugin."""
    code, known = _pin_source(pid)
    if not known:
        return _tree_hash(code) if code else None
    return _digest((known["entry_hash"] + (_tree_hash(code) if code else "")).encode())


def pin_covers_code(pid):
    """Whether a pin on pid hashes code, not only a remote source's marketplace entry."""
    return bool(_pin_source(pid)[0])


def installed():
    """{id: {"path", "enabled"}} for installed plugins, enabled as Claude Code resolves it here: user settings, then
    this project's .claude/settings.json and settings.local.json."""
    data = (_json(os.path.join(claude_dir(), "plugins", "installed_plugins.json"), {}) or {}).get("plugins") or {}
    root = c.git_root(os.getcwd()) or os.getcwd()
    enabled = {}
    for f in (os.path.join(claude_dir(), "settings.json"), os.path.join(root, ".claude", "settings.json"),
              os.path.join(root, ".claude", "settings.local.json")):
        enabled.update((_json(f, {}) or {}).get("enabledPlugins") or {})
    out = {}
    for pid, entries in data.items():
        entries = [e for e in (entries if isinstance(entries, list) else [entries]) if isinstance(e, dict)]
        # a stale entry (another scope, a removed checkout) must not hide the copy that is really there
        entry = next((e for e in entries if e.get("installPath") and os.path.isdir(e["installPath"])),
                     entries[0] if entries else {})
        out[pid] = {"path": entry.get("installPath"), "enabled": bool(enabled.get(pid, False))}
    return out


def find(need, n=10, skipped=None):
    words = [w for w in re.findall(r"[a-z0-9+#.-]+", need.lower()) if w not in _STOP]
    if not words:
        return []
    have, hits, ledger = installed(), [], _json(_trust_path(), {})
    for p in index(skipped):
        name, text = p["name"].lower(), f"{p['description']} {p['category']}".lower()
        # a short word must be a whole word ("go" is not google or mongo); longer ones may prefix ("test" → testing)
        pats = [re.compile(r"\b" + re.escape(w) + (r"\b" if len(w) <= 3 else "")) for w in words]
        score = sum(3 * bool(r.search(name)) + bool(r.search(text)) for r in pats)
        if score:
            trusted, why = trust(p, ledger)
            tree = p["local"] or have.get(p["id"], {}).get("path")
            # T-1392: what it runs and adds, read from a tree on disk; None for a remote entry not installed
            prof = profile(tree)
            needs = capabilities(tree, prof) if tree and os.path.isdir(tree) else None
            hits.append(dict(p, score=score, installed=p["id"] in have, trusted=trusted, untrusted=why,
                             enabled=have.get(p["id"], {}).get("enabled", False), tokens=prof["tokens"],
                             needs=needs and {k: v for k, v in needs.items() if k not in ("handlers", "mcp_specs", "lsp_specs",
                                                                              "fm_hook_specs")}))
    return sorted(hits, key=lambda h: (-h["score"], h["id"]))[:n]


def _own_findings(pid, prof):
    """Conflicts a plugin brings on its own: a Stop hook, process skills that duplicate Sampo."""
    found = []
    if prof["sync_stop"]:
        found.append({"plugin": pid, "kind": "stop-hook", "detail": "a Stop hook that can hold the turn open: it competes with Sampo's "
                      "evidence gate and drive for when a turn may end",
                      "action": f"fm plugins disable {pid} (after fm ask ID plugin)"})
    for name, _ in prof["skills"] + prof["commands"]:
        owner = next((b for b, keys in FOREMAN_OWNS.items() if any(k in name.lower() for k in keys)), None)
        if owner:
            found.append({"plugin": pid, "kind": "overlaps-foreman", "detail": f"{name} duplicates Sampo's {owner}",
                          "action": "use Sampo's; disable the plugin if both keep triggering"})
    return found


def check(only=None):
    """Conflicts among enabled plugins and with Sampo: [{plugin, kind, detail, action}]."""
    found, mcp = [], {}
    for pid, info in sorted(installed().items()):
        if not info["enabled"] or pid.split("@")[0] in c.NAMESPACES or (only and pid != only):
            continue
        prof = profile(info["path"])
        if pid.split("@")[0] in KNOWN_CONFLICTS:
            found.append({"plugin": pid, "kind": "known-conflict", "detail": KNOWN_CONFLICTS[pid.split("@")[0]],
                          "action": f"fm plugins disable {pid} (after fm ask ID plugin)"})
        found += _own_findings(pid, prof)
        for server, spec in prof["mcp"].items():
            key = (server, json.dumps(spec, sort_keys=True))
            if server in {k[0] for k in mcp} or key in mcp:
                first = next(v for k, v in mcp.items() if k[0] == server or k == key)
                found.append({"plugin": pid, "kind": "duplicate-mcp", "detail": f"MCP server {server!r} is also "
                              f"provided by {first}", "action": f"keep one: claude plugin disable {pid} or {first}"})
            mcp.setdefault(key, pid)
    return found


# T-0205: words in a skill's name or description that say it fits a stage, for fm next's hint. Claude Code's own
# skills for a stage come after the installed ones.
# T-0252: the tables live in skills/routing.json, where fm evolve can tune them against the bench
_ROUTES = c.routing()
STAGE_WORDS = {k: tuple(v) for k, v in (_ROUTES.get("stage_words") or {}).items()}
UI_WORDS = tuple(_ROUTES.get("ui_words") or ())
UI_FILES = c.lazy(r"\.(tsx|jsx|vue|svelte|css|scss|html)\b")
BUILTIN = {k: tuple(v) for k, v in (_ROUTES.get("builtin_skills") or {}).items()}
LENS_WORDS = {k: tuple(v) for k, v in (_ROUTES.get("lens_words") or {}).items()}  # T-0276: review skills per lens
LENS_BUILTIN = {k: tuple(v) for k, v in (_ROUTES.get("lens_builtin") or {}).items()}
INDEX_VERSION = 2  # bump when what the index keeps changes


def _gist(name, desc):
    """What a skill is for: its name and its description's first sentence (later sentences list side uses: a skill
    creator "benchmarks skill performance" without being a performance tool)."""
    first = re.split(r"(?<=[.!?])\s", desc.strip(), maxsplit=1)[0]  # outside the f-string: Python 3.11
    return f"{name} {first}".lower()


def _skill_index(p):
    """[{name, text}] for the skills and commands Claude can invoke here (enabled plugins', the user's, the project's)
    and how often each was invoked in this project, cached in the project's state until the plugin registry, the
    settings or a skills folder changes, or the day does."""
    root = p.root
    watch = [os.path.join(claude_dir(), "plugins", "installed_plugins.json"), os.path.join(claude_dir(), "settings.json"),
             os.path.join(root, ".claude", "settings.json"), os.path.join(root, ".claude", "settings.local.json"),
             os.path.join(claude_dir(), "skills"), os.path.join(root, ".claude", "skills")]
    key = [INDEX_VERSION, c.now()[:10]] + [os.path.getmtime(f) if os.path.exists(f) else 0 for f in watch]
    path = os.path.join(p.dir, "skill-index.json")
    cached = _json(path, {}) or {}
    if cached.get("key") == key:
        return cached
    skills = []
    for pid, info in sorted(installed().items()):
        if info["enabled"] and pid.split("@")[0] not in c.NAMESPACES:
            prof = profile(info["path"])
            skills += [{"name": f"/{pid.split('@')[0]}:{n}", "text": _gist(n, d)} for n, d in prof["skills"] + prof["commands"]]
    for base in watch[4:]:
        for f in sorted(glob.glob(os.path.join(base, "*", "SKILL.md"))):
            fm = _frontmatter(f)
            name = fm.get("name") or os.path.basename(os.path.dirname(f))
            skills.append({"name": f"/{name}", "text": _gist(name, fm.get("description", ""))})
    uses = {}
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 5000):
        if e.get("tool") == "Skill" and e.get("kind") == "tool" and e.get("project") == p.slug:
            uses[str(e.get("target"))] = uses.get(str(e.get("target")), 0) + 1
    data = {"key": key, "skills": skills, "uses": uses}
    try:
        c.write_atomic(path, json.dumps(data))
    except OSError:
        pass
    return data


def stage_skills(p, b, n=3):
    """The installed skills that fit a task's stage, ones used before first, then Claude Code's own (T-0205)."""
    idx = _skill_index(p)
    words = STAGE_WORDS.get(b.type, ())
    ui = UI_WORDS if any(UI_FILES.search(str(s)) for s in b.meta.get("scope") or []) else ()
    owned = [k for keys in FOREMAN_OWNS.values() for k in keys]  # Foreman's own process stays Foreman's
    hits = []
    for e in idx.get("skills") or []:
        score = sum(w in e["text"] for w in words + ui)
        meta = re.search(r"skill|plugin", e["name"].lower())  # tools for authoring Claude extensions, not the work
        if score and not meta and not any(k in e["name"].lower() for k in owned):
            hits.append((-idx["uses"].get(e["name"][1:], 0), -score, e["name"]))
    names = [h[2] for h in sorted(hits)][:n]
    return names + [f"/{x}" for x in BUILTIN.get(b.type, ()) if f"/{x}" not in names][:max(0, n - len(names))]


def lens_skills(p, lenses, n=2):
    """T-0230/T-0276: {lens: [skill names]} — installed review skills whose name or description fits each audit lens,
    then Claude Code's own; their findings can be recorded as that lens."""
    idx = _skill_index(p)
    owned = [k for keys in FOREMAN_OWNS.values() for k in keys]
    out = {}
    for lens in lenses:
        words = LENS_WORDS.get(lens, ())
        hits = sorted((-sum(w in e["text"] for w in words), e["name"]) for e in idx.get("skills") or []
                      if any(w in e["text"] for w in words) and not re.search(r"skill|plugin", e["name"].lower())
                      and not any(k in e["name"].lower() for k in owned))
        names = [h[1] for h in hits][:n]
        names += [f"/{x}" for x in LENS_BUILTIN.get(lens, ()) if f"/{x}" not in names]
        if names:
            out[lens] = names
    return out


def _manifest(key, add=None, drop=None):
    """uninstall-user's record of what fm plugins added (`plugins_installed`, `marketplaces_added`). True if changed."""
    path = os.path.join(c.state_dir(), "install-manifest.json")
    m = _json(path, {}) or {}
    old = list(m.get(key) or [])
    new = list(dict.fromkeys(x for x in old + ([add] if add else []) if x != drop))
    if new == old:
        return False
    m[key] = new
    c.write_atomic(path, json.dumps(m, indent=2) + "\n")
    return True


def _claude(*args, timeout=300):
    try:
        r = subprocess.run(["claude", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise c.PolicyError("the claude CLI isn't on PATH")
    if r.returncode != 0:
        raise c.PolicyError(f"claude {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def _marketplaces():
    return {_marketplace_name(_json(f, {}), os.path.dirname(os.path.dirname(f))) for f in _marketplace_files()}


def add_marketplace(source):
    """Add a marketplace (GitHub owner/repo, git URL or local path); a new one is recorded for uninstall-user.
    Returns the names it added."""
    before = _marketplaces()
    _claude("plugin", "marketplace", "add", source)
    new = sorted(_marketplaces() - before)
    for name in new:
        _manifest("marketplaces_added", add=name)
    return new


def install(pid):
    """Install (or, when installed but disabled, enable) a plugin; a fresh install is recorded for uninstall-user.
    Returns (what was done, conflicts it brings)."""
    known, have = {p["id"]: p for p in index()}, installed()
    if pid not in known and pid not in have:
        return None, []
    conflicts = _own_findings(pid, profile((known.get(pid) or {}).get("local") or have.get(pid, {}).get("path")))
    fresh = pid not in have
    _claude("plugin", "install", pid, "--scope", "user") if fresh else _claude("plugin", "enable", pid)
    if fresh:
        if pid not in installed():  # exit 0 without an install (e.g. a prompt it couldn't show) owns nothing
            raise c.PolicyError(f"claude plugin install exited 0 but {pid} isn't installed")
        _manifest("plugins_installed", add=pid)
    record_trust(pid, fresh)  # T-1396: this identity; a later rename or re-source reads as untrusted
    return ("installed" if fresh else "enabled"), conflicts


def _runs_line(needs):
    """T-1392: what a find hit runs on its own and adds to the session, as indented lines; a remote entry has no tree
    to read until installed."""
    if needs is None:
        return "\n    runs: unknown until installed"
    part = lambda keys: " · ".join(
        f"{label} {', '.join(c.plain(x)[:40] for x in needs[k][:6])}" + (" …" if len(needs[k]) > 6 else "")
        for k, label in keys if needs.get(k))
    runs = part((("hooks", "hook"), ("fm_hooks", "hook"), ("modules", "hooks module"), ("mcp", "MCP"),
                 ("lsp", "LSP"), ("scripts", "script")))
    adds = part((("commands", "command"), ("agents", "agent"), ("skills", "skill")))
    return f"\n    runs: {runs or 'nothing on its own'}" + (f"\n    adds: {adds}" if adds else "")


def suggest(root, n=3):
    """T-1397: up to n marketplace plugins not installed that fit the project's own stack: for each card fm lib
    detects here (strongest first), the best find() hit for its name, with the card and its evidence as the reason.
    Listed only: installing still needs fm ask ID plugin and fm plugins install."""
    from sampo import lib
    out, seen = [], set()
    for card in lib.suggest(root, n=12):
        area = card.get("area") if card.get("area") not in (None, "lang") else ""  # cargo's area is rust; lang says nothing
        for h in find(f"{card['name']} {area}", n=3):
            if h["installed"] or h["id"] in seen:
                continue
            why = ", ".join(str(w) for w in (card.get("why") or [])[:2])  # not a list: two reason tags
            out.append({"id": h["id"], "description": h["description"], "tokens": h["tokens"], "trusted": h["trusted"],
                        "reason": f"the project uses {card['name']}" + (f", {area}" if area else "")
                                  + (f" ({why})" if why else "")})
            seen.add(h["id"])
            break
        if len(out) >= n:
            break
    return out


def cmd_plugins(args):
    from sampo import cli
    if args.action == "suggest":  # T-1397
        rows = suggest(os.getcwd())
        size = lambda r: f"~{r['tokens']:,} tokens" if r["tokens"] else "size known once installed"  # noqa: E731
        return cli.out(args, rows, "\n".join(
            [f"{r['id']} — {c.plain(r['reason'])} ({size(r)}): {c.plain(r['description'])[:100]}" for r in rows] +["Install one only after fm ask ID plugin --pin ID (nothing was installed)." if rows else
                               "No marketplace plugin fits this project's stack beyond what's installed."]))
    if args.action in ("add-marketplace", "forget") and not args.words:
        raise cli.UsageError(f"fm plugins {args.action} needs " + (
            "a source (owner/repo, git URL or path)" if args.action == "add-marketplace" else "an id"))
    if args.action == "add-marketplace":
        new = add_marketplace(args.words[0])
        return print((f"Added marketplace {', '.join(new)}" if new else f"{args.words[0]} was already known")
                     + "; fm plugins find <what you need> searches it.")
    if args.action == "forget":  # uninstall-user leaves it in place
        dropped = [k for k in ("plugins_installed", "marketplaces_added") if _manifest(k, drop=args.words[0])]
        return print(f"uninstall-user will keep {args.words[0]}." if dropped else
                     f"{args.words[0]} wasn't added by fm plugins; nothing to forget.")
    if args.action in ("install", "enable", "disable"):
        pid = args.words[0] if args.words else ""
        if args.action == "disable":
            _claude("plugin", "disable", pid, timeout=120)
            return print(f"{pid} disabled (/reload-plugins to apply)")
        done, conflicts = install(pid)
        if not done:
            raise cli.UsageError(f"{pid!r} isn't in the known marketplaces; fm plugins find <what you need> lists them")
        return print(f"{pid} {done} (/reload-plugins to load it)." + "".join(
            f"\n  note: {f['detail']} → {f['action']}" for f in conflicts))
    if args.action == "find":
        skipped = []
        hits = find(" ".join(args.words), skipped=skipped)
        cli.out(args, hits, "\n".join(
            f"{h['id']:<40} {'enabled' if h['enabled'] else 'installed' if h['installed'] else 'available':<9} "
            f"~{h['tokens']} tok  {h['description'][:70]}" + (f"\n    untrusted: {h['untrusted']}" if h["untrusted"] else "")
            + _runs_line(h["needs"]) for h in hits) or "No matching plugin in the known marketplaces.")
        if skipped and not getattr(args, "json", False):  # T-1396: a bad entry is left out with its reason, no crash
            print(f"({len(skipped)} marketplace entries skipped: {'; '.join(skipped[:3])}"
                  + (f"; {len(skipped) - 3} more)" if len(skipped) > 3 else ")"))
    elif args.action == "check":
        found = check(args.words[0] if args.words else None)
        cli.out(args, found, "\n".join(f"{f['plugin']}: {f['detail']} → {f['action']}" for f in found)
                  or "No conflicts among enabled plugins.")
