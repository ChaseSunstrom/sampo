"""T-3672: the user's machine-local profile, so every agent starts knowing who it works for without anyone telling it.

It lives in the state folder (state/profile/, git-ignored; never a repo, never the public plugin's CLAUDE.md):
seed.md is the user's own lines, imported once from the <!-- sampo-profile:begin/end --> block of Claude's CLAUDE.md
when present (`sampo profile edit` changes it); profile.md (0600) is built from the seed plus Claude's auto-memories
(<claude config>/projects/*/memory/*.md): every memory typed user or feedback, and any other memory whose name recurs in
two or more projects (a fact about one project is that project's, not the user's), ranked seed, user, feedback,
recurring, newest first, deduped by name and text. No model call. A fresh install has no profile and injects nothing.

Privacy: everything written is scrubbed (secrets, the user's private terms, home paths, every IPv4, emails and
user@host, every domain past a short public allowlist, the machine's logins) and `check()` refuses any that remain;
the digest an agent gets is empty while one does. Where it goes: SessionStart in any folder (hooks.session_start,
opencode through the same handler), SubagentStart (the short digest), lanes.builder_brief, smith's prompts, the MCP
tool `profile` and resource sampo://profile; Codex and Gemini only by `sampo profile link codex|gemini` (opt-in)."""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time

from sampo import core as c

DIGEST_MAX = 600   # bytes a session's digest may take (SessionStart, the builder brief)
BRIEF_MAX = 320    # a subagent's (SubagentStart): every subagent pays it
FULL_MAX = 4000    # the whole profile (sampo profile show, the MCP resource)
LIVE_MAX = 200     # the live line naming other sessions' projects
ITEM_MAX = 110     # one item in a digest
CHECK_S = 600      # a session start refreshes the profile at most this often (a detached rebuild --if-stale)
BEGIN, END = "<!-- sampo-profile:begin -->", "<!-- sampo-profile:end -->"
HEAD = "# The user (machine-local profile: sampo profile)"
DIGEST_HEAD = "The user's standing preferences (machine-local; all: sampo profile show):"
NEW_FOLDER = ("This folder is new to Sampo: `sampo init` registers it (git init first for lanes and history); "
              "`sampo next` then says what to do.")
_PUBLIC = ("github.com", "gitlab.com", "anthropic.com", "claude.ai", "claude.com")
_TLDS = ("com|net|org|io|dev|ai|app|co|me|us|uk|de|fr|nl|eu|ca|au|jp|info|biz|xyz|cloud|site|online|tech|test|local|"
         "lan|home|internal|localdomain|arpa|example|invalid|corp|intranet|private")
_DOMAIN = c.lazy(r"(?i)(?<![\w.@-])(?:[a-z0-9-]+\.)+(?:" + _TLDS + r")(?![\w-]|\.[a-z0-9])")
_EMAIL = c.lazy(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*")
_FRONT = c.lazy(r"\A---\n(.*?)\n---[ \t]*\n?(.*)", re.S)
_NOT_LOGINS = {"root", "user", "admin", "runner", "ubuntu", "host", "home"}


def folder():
    return os.path.join(c.state_dir(), "profile")


def path():
    return os.path.join(folder(), "profile.md")


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _write(p, text):
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    c.write_atomic(p, text)
    os.chmod(p, 0o600)


# ---------------------------------------------------------------- privacy

def _logins(deep=False):
    """The machine's login names (whole words, 3+ characters); deep adds the GitHub owner of Sampo's own origin, one
    git call, so only rebuild and check pay it (never a hook)."""
    names = {getpass.getuser(), os.path.basename(os.path.expanduser("~").rstrip("/"))}
    if deep:
        try:
            url = subprocess.run(["git", "-C", c.PLUGIN_ROOT, "remote", "get-url", "origin"], capture_output=True,
                                 text=True, timeout=5).stdout
            m = re.search(r"(?:github|gitlab)\.com[:/]([\w.-]+)/", url)
            if m:
                names.add(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted({n.lower() for n in names if n and len(n) >= 3 and n.lower() not in _NOT_LOGINS})


def _login_re(logins):
    return re.compile(r"(?i)(?<![\w])(?:" + "|".join(map(re.escape, logins)) + r")(?![\w])") if logins else None


def _public(host):
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in _PUBLIC)


def _home(text):
    home = os.path.expanduser("~").rstrip("/")
    return bool(c._HOMES.search(text) or c._CLAUDE_HOMES.search(text) or (len(home) > 1 and home in text))


def leaks(text, deep=False):
    """The kinds of private data the text holds (names only, never the values)."""
    rx = _login_re(_logins(deep))
    found = {"secret": bool(c.secret_kinds(text)), "private": c.has_private(text), "home": _home(text),
             "ip": bool(c._IPV4.search(text)), "email": bool(_EMAIL.search(text)),
             "domain": any(not _public(m.group(0)) for m in _DOMAIN.finditer(text)),
             "login": bool(rx and rx.search(text))}
    return [k for k, v in found.items() if v]


def scrub(text, deep=True):
    """text with everything leaks() looks for replaced: <host>, <user>, ~, <private>, the secret redactions."""
    text = c.redact_local(c.redact(text))
    text = c._IPV4.sub("<host>", text)
    text = _EMAIL.sub("<user>@<host>", text)
    text = _DOMAIN.sub(lambda m: m.group(0) if _public(m.group(0)) else "<host>", text)
    rx = _login_re(_logins(deep))
    return rx.sub("<user>", text) if rx else text


# ---------------------------------------------------------------- sources

def _claude_md():
    from sampo import claude
    return os.path.join(claude.config_dir(), "CLAUDE.md")


def seed_items():
    """The seed's items: each top-level '- ' line with its indented lines, one line each."""
    seed = os.path.join(folder(), "seed.md")
    text = _read(seed)
    if text is None:  # imported once; from then on seed.md is the user's (sampo profile edit)
        block = re.search(re.escape(BEGIN) + r"(.*?)" + re.escape(END), _read(_claude_md()) or "", re.S)
        if not block:
            return []
        text = block.group(1).strip() + "\n"
        _write(seed, text)
    items = []
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith(("#", "<!--")):
            continue
        if ln.startswith("- "):
            items.append(ln[2:].strip())
        elif items:
            items[-1] += " " + re.sub(r"^\s*(?:[-*]\s+)?", "", ln).strip()
    return items


def _unquote(s):
    """A front-matter scalar's text: "…" read as JSON (YAML's double-quoted escapes), '…' with '' as one quote."""
    s = s.strip()
    if len(s) > 1 and s[0] == s[-1] == '"':
        try:
            got = json.loads(s)
            return got if isinstance(got, str) else s
        except ValueError:
            return s[1:-1]
    if len(s) > 1 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _encoded(p):
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def memories():
    """[(rank, -mtime, name, text)] of Claude's memories worth the profile, sensitive projects' folders skipped."""
    from sampo import claude
    root = os.path.join(claude.config_dir(), "projects")
    hidden = [_encoded(p.root) for p, m in c.all_projects() if m.get("sensitive")]
    seen, folders = {}, {}
    try:
        dirs = sorted(os.listdir(root))
    except OSError:
        return []
    for d in dirs:
        if any(d == h or d.startswith(h + "-") for h in hidden):
            continue
        mem = os.path.join(root, d, "memory")
        try:
            names = sorted(n for n in os.listdir(mem) if n.endswith(".md") and n != "MEMORY.md")
        except OSError:
            continue
        for n in names:
            f = os.path.join(mem, n)
            m = _FRONT.match(_read(f) or "")
            if not m:
                continue
            head, body = m.groups()
            kind = re.search(r"(?m)^\s*type:\s*(\w+)", head)
            desc = re.search(r"(?m)^description:\s*(.+)", head)
            name = re.search(r"(?m)^name:\s*(.+)", head)
            key = _unquote(name.group(1) if name else n[:-3]).lower()
            text = _unquote(desc.group(1)) if desc else next(
                (x.strip() for x in body.splitlines() if x.strip()), "")
            if not text:
                continue
            folders.setdefault(key, set()).add(d)
            rank = {"user": 1, "feedback": 2}.get(kind.group(1).lower() if kind else "", 3)
            try:
                mt = os.stat(f).st_mtime
            except OSError:
                mt = 0
            if key not in seen or (rank, -mt) < seen[key][:2]:
                seen[key] = (rank, -mt, key, text)
    return sorted(v for k, v in seen.items() if v[0] < 3 or len(folders[k]) > 1)


def _sources():
    """Every file the profile is built from (for staleness)."""
    from sampo import claude
    import glob
    return [os.path.join(folder(), "seed.md"), _claude_md()] + glob.glob(
        os.path.join(claude.config_dir(), "projects", "*", "memory", "*.md"))


def stale():
    try:
        built = os.stat(path()).st_mtime
    except OSError:
        return True
    for f in _sources():
        try:
            if os.stat(f).st_mtime > built:
                return True
        except OSError:
            continue
    return False


def build():
    """(profile text, item count): the seed's items, then the memories', scrubbed, deduped, cut to FULL_MAX."""
    lines, keys = [], set()
    size = len(HEAD) + 1
    for text in seed_items() + [m[3] for m in memories()]:
        line = "- " + c.fit(" ".join(scrub(text).split()), 300)
        k = re.sub(r"\W+", "", line.lower())[:60]
        if k in keys:
            continue
        if size + len(line) + 1 > FULL_MAX:
            break
        keys.add(k)
        lines.append(line)
        size += len(line) + 1
    return (HEAD + "\n" + "\n".join(lines) + "\n", len(lines)) if lines else ("", 0)


def rebuild(if_stale=False):
    """Writes profile.md (and refreshes linked copies); None when nothing was built (no seed, no memories)."""
    if if_stale and not stale():
        try:
            os.utime(path())  # checked now: the next session start waits CHECK_S before asking again
        except OSError:
            pass
        return None
    text, n = build()
    if not text:
        return None
    if _read(path()) != text or if_stale:
        _write(path(), text)
    for tool in _links():
        try:
            link(tool)
        except c.PolicyError:
            pass
    return {"path": path(), "items": n, "chars": len(text)}


# ---------------------------------------------------------------- what agents get

def items():
    text = _read(path()) or ""
    return [ln[2:] for ln in text.splitlines() if ln.startswith("- ")]


def digest(short=False):
    """The items that fit DIGEST_MAX (short: BRIEF_MAX) bytes under a header; '' with no profile or a leak in it."""
    text = _read(path())
    if not text or leaks(text):
        return ""
    cap, out = BRIEF_MAX if short else DIGEST_MAX, DIGEST_HEAD
    for it in items():
        line = "\n- " + c.fit(it, ITEM_MAX if not short else 80)
        if len((out + line).encode()) > cap:
            break
        out += line
    return out if out != DIGEST_HEAD else ""


def full():
    text = _read(path())
    return text if text and not leaks(text) else ""


def live_line(sid=None):
    """The projects other live sessions work in (names only; a sensitive project is never named)."""
    try:
        from sampo import bus
        snaps = bus.live()
    except Exception:  # noqa: BLE001 — a note, never a failed start
        return ""
    counts = {}
    for s in snaps:
        slug = s.get("project")
        if s.get("session_id") != sid and isinstance(slug, str) and slug:
            counts[slug] = counts.get(slug, 0) + 1
    if counts:
        hidden = {p.slug for p, m in c.all_projects() if m.get("sensitive")}
        counts = {k: v for k, v in counts.items() if k not in hidden}
    if not counts:
        return ""
    names = ", ".join(f"{k} ({n})" if n > 1 else k for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return c.fit(f"Other live sessions work in: {names} (sampo bus send reaches them).", LIVE_MAX)


def note(sid=None, new_folder=False, short=False):
    """What an agent starting now is told: the digest, then (a session) the new-folder line and the live line."""
    d = digest(short)
    if not d:
        return ""
    return "\n".join(filter(None, [d, NEW_FOLDER if new_folder else "", "" if short else live_line(sid)]))


def refresh():
    """A session start's detached `sampo profile rebuild --if-stale`, at most once per CHECK_S."""
    try:
        if time.time() - os.stat(path()).st_mtime < CHECK_S:
            return
    except OSError:
        pass
    subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "profile", "rebuild", "--if-stale"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# ---------------------------------------------------------------- check, link

def check():
    """Problems with profile.md: its size past FULL_MAX, and each line holding private data (kinds, never values)."""
    text = _read(path())
    if text is None:
        return []
    probs = [f"{len(text)} chars, over the {FULL_MAX} budget"] if len(text) > FULL_MAX else []
    for i, ln in enumerate(text.splitlines(), 1):
        kinds = leaks(ln, deep=True)
        if kinds:
            probs.append(f"line {i}: {', '.join(kinds)}")
    return probs


LINKS = "links.json"
_RULES = {"codex": "AGENTS.md", "gemini": "GEMINI.md"}


def _links():
    try:
        with open(os.path.join(folder(), LINKS)) as f:
            got = json.load(f)
    except (OSError, ValueError):
        return []
    return [t for t in got if t in _RULES] if isinstance(got, list) else []


def link(tool, off=False):
    """Opt-in: the profile in Codex's or Gemini's own rules file (their hooks, when `sampo agents install` set them up,
    carry the digest anyway); kept current by every rebuild, removed by off."""
    from sampo import agents
    if tool not in _RULES:
        raise c.PolicyError(f"sampo profile link: codex or gemini, not {tool!r} (opencode and Claude Code get it from "
                            "Sampo's hooks)")
    d = agents._config_dirs()[tool]
    if not off and not (shutil.which(tool) or os.path.isdir(d)):
        raise c.PolicyError(f"{tool} isn't installed here (no {tool} on PATH, no {d}): nothing to link")
    rules = agents._files(tool)["rules"]
    body = None if off else full().strip()
    if not off and not body:
        raise c.PolicyError("no profile to link (sampo profile rebuild builds it; sampo profile check names a leak)")
    have = _read(rules)
    if have is not None or body:
        new = agents._block(have or "", BEGIN, END, body)
        if new != (have or ""):
            os.makedirs(os.path.dirname(rules), exist_ok=True)
            c.write_atomic(rules, new)
    tools = sorted(set(_links()) - {tool} if off else set(_links()) | {tool})
    _write(os.path.join(folder(), LINKS), json.dumps(tools) + "\n")
    return rules


# ---------------------------------------------------------------- the verb

def cmd_profile(args):
    from sampo import cli
    act = args.action
    if act == "rebuild":
        got = rebuild(if_stale=args.if_stale)
        if got is None:
            return cli.out(args, {"built": False}, None if args.if_stale else
                           "nothing to build: no seed and no user or feedback memories (sampo profile edit adds lines)")
        probs = check()
        got.update(digest_bytes=len(digest().encode()), brief_bytes=len(digest(short=True).encode()))
        cli.out(args, dict(got, problems=probs),
                f"profile: {got['items']} items, {got['chars']} chars in {got['path']}; a session's digest "
                f"{got['digest_bytes']} bytes, a subagent's {got['brief_bytes']}")
        if probs:
            raise c.PolicyError("sampo profile check: " + "; ".join(probs))
        return 0
    if act == "check":
        probs = check()
        if probs:
            raise c.PolicyError("sampo profile check: " + "; ".join(probs) + " (sampo profile rebuild scrubs it)")
        return cli.out(args, {"ok": True, "chars": len(_read(path()) or "")}, "profile: clean, within budget")
    if act == "edit":
        seed = os.path.join(folder(), "seed.md")
        if not os.path.exists(seed):
            seed_items()
        if not os.path.exists(seed):
            _write(seed, "- \n")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if editor and sys.stdin.isatty():
            subprocess.run([*editor.split(), seed])
            got = rebuild()
            return cli.out(args, {"seed": seed, "built": got}, f"seed: {seed}; profile rebuilt")
        return cli.out(args, {"seed": seed}, f"the seed is {seed}: one '- ' line per preference; then sampo profile "
                                             "rebuild")
    if act == "link":
        if not args.tool:
            raise cli.UsageError("sampo profile link codex|gemini [--off]")
        rules = link(args.tool, off=args.off)
        return cli.out(args, {"tool": args.tool, "rules": rules, "linked": not args.off},
                       f"{args.tool}: profile {'removed from' if args.off else 'kept in'} {rules}")
    text = _read(path())
    if not text:
        return cli.out(args, {"profile": None}, "no profile yet: sampo profile rebuild builds one from Claude's "
                                                "user and feedback memories and the seed (sampo profile edit)")
    return cli.out(args, {"profile": path(), "text": text, "problems": check()}, text.rstrip())
