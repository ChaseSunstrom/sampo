"""fm secrets (T-0132): credentials in the working tree, in git history (--history) and in Claude Code's config, found
with fmcore's redaction patterns and named by kind and place, never printed. A line marked `pragma: allowlist secret`
(the detect-secrets convention) is skipped, so a test fixture can opt out. fm task finish --commit runs the same diff
scan over the lines it is about to commit."""
import json
import os
import re
import subprocess

from sampo import core as c

PRIVATE_KEY, JWT, TOKEN, _AUTH, _ASSIGN, URL_CRED = (rx for rx, _ in c._SECRET_RES)  # six, or this fails at import
# a literal of 8+ characters under a secret's name (DB_PASSWORD too), but not one naming a secret (SecretId, token_path)
# or a placeholder: quoted in code (`password = input()` is code), bare to the end of the line in config (.env, YAML)
_NAMED = (r"""(?i)(?<![A-Za-z0-9])(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|auth[_-]?token|token|"""
          r"""secret|password|passwd)(?!\w*(?:id|name|arn|ref|path|file|url|uri|type|field|var)s?\b)\w*["']?\s*[:=]\s*""")
LITERAL = re.compile(_NAMED + r"""["']([^"'\s$<{]{8,})["']""")
BARE = re.compile(_NAMED + r"""([^"'\s$<{#]{8,})[ \t]*(?:#.*)?$""", re.M)
CONFIG = re.compile(r"(?i)(?:^|/)\.env(?!\.(?:example|sample|template|dist)$)[^/]*$|\.(?:ya?ml|ini|cfg|conf|properties|toml|tfvars)$")
PLACEHOLDER = re.compile(r"(?i)example|placeholder|changeme|dummy|fake|redacted|your|xxxx|\*\*\*|abc123|123456")
SECRET_NAME = re.compile(r"(?i)key|token|secret|passw|pwd|auth|credential|cookie")
ALLOW = "pragma: allowlist secret"
PREFIXES = [("sk-ant-", "Anthropic key"), ("sk-", "API key (sk-)"), ("github_pat_", "GitHub token"), ("gh", "GitHub token"),
            ("AKIA", "AWS access key"), ("xox", "Slack token"), ("glpat-", "GitLab token"), ("AIza", "Google API key")]
KINDS = [(PRIVATE_KEY, "private key"), (JWT, "JWT"), (TOKEN, None), (URL_CRED, "password in a URL"), (LITERAL, "secret literal")]
# git's own config can change what a diff shows (no b/ prefix, binary or textconv'd files): T-0132 review
DIFF = ["-c", "core.quotePath=false", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false"]
DIFF_OPTS = ["-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "--src-prefix=a/", "--dst-prefix=b/"]
PROJECT_CONFIG = (".mcp.json", os.path.join(".claude", "settings.json"), os.path.join(".claude", "settings.local.json"))


# a token (sk-, ghp_, AKIA...) is random, so only a whole placeholder word or a run of x's makes it fake; a bare "your" can occur by chance
TOKEN_PLACEHOLDER = re.compile(r"(?i)example|placeholder|changeme|dummy|redacted|your[-_]|yourkey|x{6,}")


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")


def is_placeholder(value, token=False):
    """T-1273: the one rule for a fake value; the commit refusal and the close warning both ask it. A plain ISO date
    or time (token_expires = "2030-01-01") is never a credential (T-3336, T-2450's case)."""
    return bool((TOKEN_PLACEHOLDER if token else PLACEHOLDER).search(value)) or not token and bool(ISO_DATE.fullmatch(value))


URL_FAKE = {"pass", "password", "passwd", "pw", "pwd", "secret", "token"}  # T-3336: user:pass@host, as docs write it


def _fake_url_password(pw):
    return pw.lower() in URL_FAKE or is_placeholder(pw)


def scan_text(text, config=False):
    """[(line, kind)] for each line of text holding a secret (its first kind); allowlisted lines don't count."""
    lines, found = text.split("\n"), {}
    for rx, kind in KINDS + ([(BARE, "secret literal")] if config else []):
        for m in rx.finditer(text):
            n = text.count("\n", 0, m.start())
            if n in found or ALLOW in lines[n] or rx in (LITERAL, BARE, TOKEN) and is_placeholder(m.group(1 if rx != TOKEN else 0), rx == TOKEN):
                continue
            if rx == URL_CRED and _fake_url_password(m.group(0)[len(m.group(1)):-1]):
                continue
            found[n] = kind or next(k for p, k in PREFIXES if m.group(0).startswith(p))
    return sorted((n + 1, k) for n, k in found.items())


def scan_diff(diff):
    """[(path, line, kind)] for secrets in the lines a unified diff adds; hunks are read by their counts, so an added
    line that starts with `++` is never taken for a file header."""
    found, path, added, rem, add, nxt = [], None, [], 0, 0, 0

    def flush():
        if path and added:
            text = "\n".join(t for _, t in added)
            found.extend((path, added[n - 1][0], kind) for n, kind in scan_text(text, bool(CONFIG.search(path))))

    for line in diff.split("\n"):
        if rem > 0 or add > 0:
            tag, body = line[:1], line[1:]
            if tag == "+":
                added.append((nxt, body))
                nxt, add = nxt + 1, add - 1
            elif tag == "-":
                rem -= 1
            elif tag == " ":
                nxt, rem, add = nxt + 1, rem - 1, add - 1
            continue
        m = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            rem, nxt, add = int(m.group(1) or 1), int(m.group(2)), int(m.group(3) or 1)
        elif line.startswith("+++ "):
            flush()
            name = line[4:].rstrip("\r").strip('"')  # any other form is still scanned, under its own name
            path, added = (None if name == "/dev/null" else name[2:] if name.startswith("b/") else name), []
    flush()
    return found


def staged_leaks(root, files, env=None):
    """Findings in what a commit of files would take, or None when git can't show it (fails closed). `env`: another
    index to read (T-4147: a dry probe's throwaway copy)."""
    try:
        r = subprocess.run(["git", "--literal-pathspecs", "-C", root, *DIFF, "diff", "--cached", *DIFF_OPTS, "--", *files],
                           capture_output=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return None if r.returncode else scan_diff(r.stdout.decode("utf-8", "replace"))


def _tree(root):
    out = []
    listed = c._git(root, "ls-files", "-co", "--exclude-standard", "-z", timeout=300, proc=True)
    if listed.returncode:  # a failed listing is a finding, never a clean "no credentials found"
        return [{"where": "tree", "path": ".", "kind": "couldn't list the files to scan",
                 "note": c.fit(c.plain(listed.stderr.strip()), 200) or f"git exit {listed.returncode}"}]
    for rel in filter(None, listed.stdout.split("\0")):
        path = os.path.join(root, rel)
        if rel in PROJECT_CONFIG or os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > 2_000_000:
            continue  # the project's Claude config is read as config, below
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" not in data[:8192]:  # not binary
            out += [{"where": "tree", "path": rel, "line": n, "kind": k}
                    for n, k in scan_text(data.decode("utf-8", "replace"), bool(CONFIG.search(rel)))]
    return out


def _history(root, known):
    """The oldest commit that added each (path, kind) still not in the tree; streamed, so a long history stays small."""
    found, sha, chunk = {}, None, []

    def take():
        for path, line, kind in scan_diff("".join(chunk)) if sha else []:
            if (path, kind) not in known:
                found[(path, kind)] = {"where": "history", "commit": sha, "path": path, "line": line, "kind": kind}

    proc = subprocess.Popen(["git", "-C", root, *DIFF, "log", "--all", "-p", *DIFF_OPTS, "--format=%x00%h"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    for line in proc.stdout:
        if line.startswith("\0"):
            take()  # newest first: an older commit's finding replaces a newer one's
            sha, chunk = line[1:].strip(), []
        else:
            chunk.append(line)
    take()
    proc.wait()
    return list(found.values())


def _json_secrets(data, trail=""):
    """[(dotted key, kind)] for secrets in a Claude config: a secret format in any string, or a literal value under a
    secret-sounding name in an env or headers map (a `${VAR}` reference is how it should be done)."""
    out = []
    items = data.items() if isinstance(data, dict) else enumerate(data) if isinstance(data, list) else []
    for k, v in items:
        here = f"{trail}[{k}]" if isinstance(k, int) else f"{trail}.{k}" if trail else str(k)
        if not isinstance(v, str):
            out += _json_secrets(v, here)
            continue
        kinds = [kind for _, kind in scan_text(v, config=True)]  # an argument like --token=… counts
        if not kinds and trail.rsplit(".", 1)[-1] in ("env", "headers") and SECRET_NAME.search(str(k)) \
                and len(v) >= 8 and "$" not in v and not is_placeholder(v):
            kinds = ["secret literal"]
        out += [(here, kind) for kind in kinds[:1]]
    return out


def _claude(root, git):
    home = os.path.expanduser("~")
    shown = lambda path: "~" + path[len(home):] if path.startswith(home + os.sep) else os.path.relpath(path, root)
    project = [os.path.join(root, r) for r in PROJECT_CONFIG]
    out = []
    for path in [os.path.join(home, ".claude", n) for n in ("settings.json", "settings.local.json")] + project + \
            [os.path.join(home, ".claude.json")]:
        if not os.path.isfile(path) or path in project and os.path.islink(path):
            continue  # no FIFO, and no project file pointing elsewhere
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            data = json.loads(raw)
        except OSError:
            continue
        except ValueError:  # a BOM, a comment or a trailing comma: read it as text instead
            data = None
        if path.endswith(".claude.json") and isinstance(data, dict):  # Claude Code's own state: only its MCP servers
            projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
            data = {"mcpServers": data.get("mcpServers") or {},
                    "projects": {k: {"mcpServers": v.get("mcpServers") or {}} for k, v in projects.items() if isinstance(v, dict)}}
        hits = _json_secrets(data) if data is not None else [(f"line {n}", k) for n, k in scan_text(raw, config=True)]
        if not hits:
            continue
        note = "the file is readable by other users" if os.stat(path).st_mode & 0o077 else ""
        if path in project and git:
            rel = os.path.relpath(path, root)
            tracked = subprocess.run(["git", "-C", root, "ls-files", "--error-unmatch", rel], capture_output=True).returncode == 0
            ignored = subprocess.run(["git", "-C", root, "check-ignore", "-q", rel], capture_output=True).returncode == 0
            note = "committed to git" if tracked else "" if ignored else "not git-ignored"
        out += [{"where": "claude", "path": shown(path), "key": key, "kind": kind, "note": note} for key, kind in hits]
    for path in (os.path.join(home, ".claude", ".credentials.json"), os.path.join(home, ".claude.json")):
        if os.path.isfile(path) and os.stat(path).st_mode & 0o077:
            out.append({"where": "claude", "path": shown(path), "kind": "credentials file",
                        "note": "readable by other users (chmod 600)"})
    return out


def audit(root, history=False):
    """Every finding for the project at root (a git checkout or any folder) and the user's Claude config."""
    git = bool(c.git_root(root))
    tree = _tree(root) if git else []
    past = _history(root, {(f["path"], f["kind"]) for f in tree}) if git and history else []
    return tree + past + _claude(root, git)


def describe(f):
    where = (f"history {f['commit']} {f['path']}" if f["where"] == "history" else
             f"{f['path']}:{f['line']}" if f.get("line") else f"{f['path']}" + (f" {f['key']}" if f.get("key") else ""))
    return f"{where} — {f['kind']}" + (f"; {f['note']}" if f.get("note") else "")


def cmd_secrets(args):
    from sampo import cli
    git = c.git_root(os.getcwd())
    found = audit(git or os.getcwd(), args.history)
    scope = ("the working tree, history and Claude config" if args.history else
             "the working tree and Claude config; --history adds git history") if git else "Claude config; not a git checkout"
    text = (f"{len(found)} finding(s); no value is printed. A real credential needs rotating (deleting it doesn't revoke "
            f"it); a test fixture's line can be marked `{ALLOW}`.\n" + "\n".join(describe(f) for f in found)) if found else \
        f"No credentials found ({scope})."
    cli.out(args, {"findings": found}, text)
    return 1 if found else 0
