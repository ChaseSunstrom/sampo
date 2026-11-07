"""fm docs: markdown that drifted from the repo — paths that don't exist, npm/make commands that aren't defined.

Report-only. Reuses tidy's detectors; paths may be relative to the file or to the repo root, gitignored
(machine-specific) paths and fenced code blocks are skipped.
"""
import json
import os
import re
import shutil
import stat
import subprocess

from sampo.tidy import _PATH_TOKEN, _looks_like_path, dead_paths, stale_commands

SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__"}
_FENCE = re.compile(r"```.*?```", re.S)
# T-1817: paths in other trees: a stack card's are the detected project's (./gradlew), a changelog's files as they were
_FRONT = re.compile(r"---\n(.*?)\n---\n", re.S)
_HISTORY = re.compile(r"(?i)(changelog|changes|history|news)(\.\w+)?\.md")
_SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")  # T-2124: fm find scans untrusted trees


def _markdown(root):
    """The repo's markdown: tracked, or untracked and not ignored (ignored notes, like Sampo's state/, are
    point-in-time records, not docs). Outside git, every .md not in a skipped or hidden folder."""
    try:
        r = subprocess.run(["git", "-C", root, *_SAFE, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                            "--", "*.md"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        r = None
    if r and r.returncode == 0:
        rels = sorted({f for f in r.stdout.split("\0") if f})
        yield from (os.path.join(root, f) for f in rels
                    if not any(part in SKIP_DIRS or part.startswith(".") for part in f.split("/")[:-1])
                    and regular(os.path.join(root, f)))
        return
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        yield from (os.path.join(d, f) for f in sorted(files) if f.endswith(".md") and regular(os.path.join(d, f)))


def regular(path):
    """A regular file, not a link: a link may point at /dev/zero, a FIFO or a file outside the tree (fm find)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _ignored(root, rels):
    if not rels:
        return set()
    try:
        p = subprocess.run(["git", "-C", root, *_SAFE, "check-ignore", "--stdin"], input="\n".join(rels),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(p.stdout.splitlines())


def _top_level(root):
    """Top-level names that hold repo content (tracked, or untracked and not ignored); every name outside git. A
    folder with only ignored files (Claude Code's .claude/scheduled_tasks.lock) doesn't make `.claude/…` a repo path."""
    try:
        r = subprocess.run(["git", "-C", root, *_SAFE, "ls-files", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return {line.split("/", 1)[0] for line in r.stdout.splitlines() if line}
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return set(os.listdir(root))
    except OSError:
        return set()


def _anchored(tok, root, here, tops):
    """Only paths that clearly point into this repo: ./ or ../, or a first component that is repo content at the root
    or exists next to the file. Bare names, other repos (org/repo), branches and domains are too ambiguous."""
    if tok.startswith(("./", "../")):
        return True
    first = tok.split("/", 1)[0]
    return "/" in tok and bool(first) and (first in tops or (here != root and os.path.exists(os.path.join(here, first))))


def scan(root):
    findings, tops = [], _top_level(root)
    for md in _markdown(root):
        try:
            with open(md, encoding="utf-8", errors="replace") as f:
                text = _FENCE.sub("", f.read(1_000_000))  # ponytail: a doc past 1 MB is checked in its first MB
        except OSError:
            continue
        front = _FRONT.match(text)
        if front and re.search(r"(?m)^detect:", front.group(1)) or _HISTORY.fullmatch(os.path.basename(md)):
            continue
        rel, here = os.path.relpath(md, root), os.path.dirname(md)
        missing = [t for t in dead_paths(text, here) if _anchored(t, root, here, tops)
                   and not os.path.exists(os.path.join(root, t))]
        ignored = _ignored(root, missing)
        findings += [{"file": rel, "kind": "path", "detail": t} for t in missing if t not in ignored]
        findings += [{"file": rel, "kind": "command", "detail": cmd} for cmd in stale_commands(text, root)]
    return findings


# T-2381: fm prove --docs: what the docs say about the repo's own CLI, checked against its files (static: any tree)
DOC_EXT = (".md", ".markdown", ".mdx", ".rst", ".adoc", ".txt")
CORPUS_FILES, CORPUS_FILE, CORPUS_BYTES = 4000, 1 << 18, 8 << 20  # the non-doc text a claim is searched in
_SPAN = re.compile(r"```[^\n]*\n(.*?)```|`([^`\n]+)`", re.S)
_STOP = {"|", "||", "&&", ";", "&", ">", ">>", "<", "2>&1"}
_NPM, _MAKE = re.compile(r"\b(?:npm run|pnpm run|yarn run|bun run)\s+([\w:.-]+)"), re.compile(r"`make\s+([\w.-]+)")
_FLAG = re.compile(r"--[A-Za-z][\w-]*")
_SUB = re.compile(r"[a-z][a-z0-9_-]*")
_SHELLY = re.compile(r"[<>\[\]{}$*|;&\\]|\.\.\.")


def _tree_files(root):
    """Repo files (tracked, or untracked and not ignored; every file outside git), relative, sorted."""
    try:
        r = subprocess.run(["git", "-C", root, *_SAFE, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        r = None
    if r and r.returncode == 0:
        return sorted({f for f in r.stdout.split("\0") if f})
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        out += [os.path.relpath(os.path.join(d, f), root) for f in sorted(files)]
    return out


def _load(root, rel, kind):
    path = os.path.join(root, rel)
    if not regular(path):
        return {}
    try:
        with open(path, "rb") as f:
            raw = f.read(1 << 20)
        if kind == "toml":
            import tomllib
            d = tomllib.loads(raw.decode("utf-8", "replace"))
        else:
            d = json.loads(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    return d if isinstance(d, dict) else {}


def _dict(x):
    return x if isinstance(x, dict) else {}


def cli_names(root, rels):
    """{name: (kind, target)} of the commands the repo itself ships, from its manifests: pyproject scripts ('py',
    'pkg.mod:func'), package.json bin ('node', path), Cargo bin targets ('cargo', None) and executables in bin/ or
    */bin/ ('bin', path). Root manifests only (ponytail: a monorepo's nested packages are not read)."""
    out = {}

    def add(name, spec):
        if isinstance(name, str) and re.fullmatch(r"[\w.-]+", name):
            out.setdefault(name, spec)
    py = _load(root, "pyproject.toml", "toml")
    for scripts in (_dict(_dict(py.get("project")).get("scripts")),
                    _dict(_dict(_dict(py.get("tool")).get("poetry")).get("scripts"))):
        for n, tgt in scripts.items():
            if isinstance(tgt, str):
                add(n, ("py", tgt.split("[")[0].strip()))
    pkg = _load(root, "package.json", "json")
    b = pkg.get("bin")
    if isinstance(b, str) and isinstance(pkg.get("name"), str):
        add(pkg["name"].rsplit("/", 1)[-1], ("node", b))
    for n, tgt in _dict(b).items():
        if isinstance(tgt, str):
            add(n, ("node", tgt))
    cargo = _load(root, "Cargo.toml", "toml")
    for t in cargo.get("bin") if isinstance(cargo.get("bin"), list) else []:
        add(_dict(t).get("name"), ("cargo", None))
    if "src/main.rs" in rels:
        add(_dict(cargo.get("package")).get("name"), ("cargo", None))
    for rel in rels:
        if re.fullmatch(r"(?:[^/]+/)?bin/[^/]+", rel) and regular(os.path.join(root, rel)) \
                and os.access(os.path.join(root, rel), os.X_OK):
            name = os.path.basename(rel)
            kind = "shim" if _shim(os.path.join(root, rel)) else "bin"
            add(name, (kind, rel))
            add(os.path.splitext(name)[0], (kind, rel))
    return out


def _shim(path):
    """A bin/ file that only hands over to another tool (bin/rails, a bundler binstub): a few lines, one of them an exec,
    require or load. Its flags and subcommands are that tool's, so the docs' claims about them are not the repo's."""
    try:
        with open(path, "rb") as f:
            lines = [x.strip() for x in f.read(4096).decode("utf-8", "replace").splitlines()]
    except OSError:
        return False
    code = [x for x in lines if x and not x.startswith("#")]
    return len(code) <= 8 and any(re.match(r"(exec|require|require_relative|load)\b", x) for x in code)


def _corpus(root, rels):
    """(text of the repo's non-doc files, rust text, python text, truncated): what a flag or subcommand must appear in."""
    parts, rust, py, size, seen = [], [], [], 0, 0
    for rel in rels:
        if rel.lower().endswith(DOC_EXT) or not regular(os.path.join(root, rel)):
            continue
        seen += 1
        if seen > CORPUS_FILES or size > CORPUS_BYTES:
            return "\n".join(parts), "\n".join(rust), "\n".join(py), True
        try:
            with open(os.path.join(root, rel), "rb") as f:
                raw = f.read(CORPUS_FILE)
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", "replace")
        size += len(text)
        parts.append(text)
        if rel.endswith(".rs"):
            rust.append(text)
        elif rel.endswith(".py"):
            py.append(text)
    return "\n".join(parts), "\n".join(rust), "\n".join(py), False


def _flag_in(name, text, rust, py):
    """A long flag the code could define: literal --name, "name" or 'name' (Go's flag, cobra), name_with_underscores
    (a dest), in a Rust tree name as a word (clap derives the flag from a field), or in a Python one a parameter of
    that name (Typer and Fire derive --verbose from `verbose`)."""
    n = name[2:]
    if name in text or f'"{n}"' in text or f"'{n}'" in text or ("-" in n and n.replace("-", "_") in text):
        return True
    w = re.escape(n.replace("-", "_"))
    return bool(rust and re.search(rf"(?<![\w-]){w}(?![\w-])", rust)
                or py and re.search(rf"(?m)^(?:[ \t]*def\s.*?|[ \t]+)(?<![\w]){w}\s*[:=,)]", py))


_PROMPT = re.compile(r"^\s*[$>]\s+")
_ELIDED = re.compile(r"\.\.\.|…|<[^>\n]+>")  # a line the docs abbreviate can't be matched
_AFTER = re.compile(r"\s*(?:[^\n`]{0,80}:[ \t]*\n\s*)?```[^\n]*\n(.*?)```", re.S)  # "prints:" and a block


def _toks(line):
    import shlex
    line = _PROMPT.sub("", line).strip()
    try:
        toks = shlex.split(line, comments=True)
    except ValueError:
        toks = line.split()
    while toks and re.fullmatch(r"\w+=\S*", toks[0]):
        toks.pop(0)
    return line, toks


def _is_command(line, names):
    """Whether a block line reads as a command (not output): a prompt, one of names, a shell builtin, a program on PATH
    or a path to one (ponytail: a heuristic, so a program-named first word of real output is read as a command)."""
    line, toks = _toks(line)
    first = toks[0] if toks else ""
    return bool(_PROMPT.match(line) or os.path.basename(first) in names or first in _BUILTINS
                or first.startswith(("./", "/", "~")) or shutil.which(first))


_BUILTINS = {"cd", "export", "source", "echo", "set", "unset", "alias", "sudo", "exit"}


def _shown(lines, i, names, prompted):
    """The output the docs show under the command on lines[i]: the lines after it up to the next command (a block with
    `$ ` prompts: up to the next prompt), blank and abbreviated lines left out."""
    out = []
    for line in lines[i + 1:]:
        if _PROMPT.match(line) if prompted else _is_command(line, names):
            break
        if line.strip() and not _ELIDED.search(line):
            out.append(line.strip())
    return out


def invocations(text, names):
    """(fenced, line, cli, rest, shell, shown) for each place the text runs one of names: a line of a fenced block or an
    inline code span that starts with the command (after a `$ ` prompt and VAR=x words). rest: its words up to a pipe,
    `&&`, `;` or a redirect; shell: the line has such syntax, a placeholder or a continuation, so it can't be run as
    written; shown: the output lines the docs print for it (after it in its block, or for a block's last command in a
    block right after that has no commands), at most 20."""
    for m in _SPAN.finditer(text):
        fenced = m.group(1) is not None
        lines = m.group(1).splitlines() if fenced else [m.group(2)]
        prompted = any(_PROMPT.match(x) and x.lstrip().startswith("$") for x in lines)
        found = []
        for i, raw in enumerate(lines):
            line, toks = _toks(raw)
            if not toks or os.path.basename(toks[0]) not in names:
                continue
            rest = []
            for t in toks[1:]:
                if t in _STOP or t.startswith((">", "2>")):
                    break
                rest.append(t)
            found.append([fenced, line, os.path.basename(toks[0]), rest, (
                len(rest) != len(toks) - 1 or any(_SHELLY.search(t) for t in rest) or line.endswith("\\")),
                _shown(lines, i, names, prompted) if fenced else []])
        nxt = _AFTER.match(text, m.end()) if fenced and found else None
        if nxt and not found[-1][5] and not any(_is_command(x, names) for x in nxt.group(1).splitlines()):
            found[-1][5] = [x.strip() for x in nxt.group(1).splitlines() if x.strip() and not _ELIDED.search(x)]
        for f in found:
            yield (*f[:5], f[5][:20])


def words(rest):
    """(subcommand path, flags) of a command's words: the leading lowercase words, and every --long flag
    (--help is every parser's own, never a claim; a value after = is dropped; after a bare -- the words are another
    tool's)."""
    path = []
    for t in rest:
        if not _SUB.fullmatch(t):
            break
        path.append(t)
    ours = rest[:rest.index("--")] if "--" in rest else rest
    flags = list(dict.fromkeys(m.group(0) for t in ours if t.startswith("--") for m in [_FLAG.match(t)]
                               if m and m.group(0) != "--help"))
    return path, flags


def claims(root, files=None):
    """(claims, cmds) of the docs (all the repo's markdown, or the absolute paths in files). A claim is {file, kind, detail,
    verdict, why}: kind path, target (a make or npm command), flag or subcommand of the repo's own CLI, or command (a
    fenced line of it). verdict, from the files alone: a path or target is proven or failed; a flag or subcommand
    the files lack fails and one they have is unproven (text in a file is not a working flag); command claims start
    unproven (fmprove.docs_proof runs them and proves what they use). cmds: the fenced commands, each {claim, cli, spec,
    rest, shell, shown} (shown: the output lines the docs print under it)."""
    rels = _tree_files(root)
    names = cli_names(root, rels)
    tops, out, cmds, seen = _top_level(root), [], [], {}
    corpus, subs = [], {}  # subs: cli -> {(file, word): found}, judged once every doc is read

    def add(rel, kind, detail, verdict, why=""):
        if (rel, kind, detail) not in seen:
            out.append(seen.setdefault((rel, kind, detail),
                                       {"file": rel, "kind": kind, "detail": detail, "verdict": verdict, "why": why}))
        return seen[(rel, kind, detail)]

    def judge(found, shim):
        """Text in a file is never a working flag: found is unproven. Not found fails, unless the search was cut short
        or the CLI is another tool's shim."""
        if found:
            return "unproven", "text in a file; a run of a documented command proves it (--yes-run)"
        why = "no non-doc file has it"
        if shim:
            return "unproven", why + " (bin/ only runs another tool, so its flags are that tool's)"
        return ("unproven", why + " (the search was cut short)") if corpus[3] else ("failed", why)
    for md in files if files is not None else _markdown(root):
        try:
            with open(md, encoding="utf-8", errors="replace") as f:
                full = f.read(1_000_000)
        except OSError:
            continue
        front = _FRONT.match(full)
        if front and re.search(r"(?m)^detect:", front.group(1)) or _HISTORY.fullmatch(os.path.basename(md)):
            continue
        rel, here = os.path.relpath(md, root), os.path.dirname(md)
        text = _FENCE.sub("", full)
        toks = [t for t in dict.fromkeys(_PATH_TOKEN.findall(text)) if _looks_like_path(t) and _anchored(t, root, here, tops)]
        ok = {t for t in toks if os.path.exists(os.path.join(here, t.rstrip("/"))) or os.path.exists(os.path.join(root, t.rstrip("/")))}
        ignored = _ignored(root, [t for t in toks if t not in ok])
        for t in toks:
            if t not in ignored:
                add(rel, "path", t, "proven" if t in ok else "failed", "" if t in ok else "no such file")
        bad = set(stale_commands(text, root))
        for cmd in [m.group(0) for m in _NPM.finditer(text)] + [f"make {m.group(1)}" for m in _MAKE.finditer(text)]:
            add(rel, "target", cmd, "failed" if cmd in bad else "proven", "not defined here" if cmd in bad else "")
        for fenced, line, cli, rest, shell, shown in invocations(full, names):
            if not corpus:
                corpus = _corpus(root, rels)
            sub, flags = words(rest)
            shim = names[cli][0] == "shim"
            if sub:
                subs.setdefault(cli, {})[(rel, sub[0])] = bool(re.search(rf"(?<![\w-]){re.escape(sub[0])}(?![\w-])", corpus[0]))
            for fl in flags:
                add(rel, "flag", fl, *judge(_flag_in(fl, *corpus[:3]), shim))
            if fenced and rest:
                detail = " ".join([cli] + rest)
                if (rel, "command", detail) not in seen:
                    cmds.append({"claim": add(rel, "command", detail, "unproven",
                                              "pipes, placeholders and continuations are not run" if shell else "not run"),
                                 "cli": cli, "spec": names[cli], "rest": rest, "shell": shell, "shown": shown})
    for cli, found in subs.items():
        # a lone lowercase word after the CLI's name may be an argument (a file), so only a CLI the docs show more than
        # one subcommand of can fail on a word it doesn't have (ponytail: the docs read stand in for the CLI's own list)
        many = len({w for _, w in found}) > 1
        for (rel, word), hit in found.items():
            v, why = judge(hit, names[cli][0] == "shim")
            if v == "failed" and not many:
                v, why = "unproven", why + " (the only subcommand the docs name: it may be an argument)"
            add(rel, "subcommand", f"{cli} {word}", v, why)
    return out, cmds


def _added_text(root, rel):
    """What the working tree adds to *rel* against HEAD: the + lines of its diff, or the whole file when git does
    not track it yet. A doc's drift is the task's only where it sits in that text (T-0035)."""
    try:
        tracked = subprocess.run(["git", "-C", root, "ls-files", "--error-unmatch", "--", rel],
                                 capture_output=True, text=True, timeout=30).returncode == 0
        if tracked:
            diff = subprocess.run(["git", "-C", root, "diff", "-U0", "HEAD", "--", rel],
                                  capture_output=True, text=True, timeout=30).stdout
            return "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    except (OSError, subprocess.SubprocessError):
        return None  # can't tell: treat all of it as the task's, as before
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def task_docs(root, docs_impact):
    """For fm task done (M/L): (blockers, notes). Docs the task says it updated must exist and add no drift; drift in
    other docs, and old drift in the ones it updated, is reported, not blocking, so an old problem never holds up
    unrelated work."""
    named = {os.path.normpath(n) for n in re.findall(r"[\w./-]+\.md\b", docs_impact or "")}
    gone = [n for n in sorted(named) if not os.path.exists(os.path.join(root, n))]
    try:  # T-1995: name the one tracked doc it most likely meant ("rules/foreman.md" for plugin/rules/foreman.md)
        tracked = subprocess.run(["git", "-C", root, "ls-files", "--", "*.md"], capture_output=True, text=True,
                                 timeout=30).stdout.splitlines() if gone else []
    except (OSError, subprocess.SubprocessError):
        tracked = []
    meant = {n: [t for t in tracked if t.endswith("/" + n)] for n in gone}
    blockers = [f"docs impact names {n}, which doesn't exist" + (f" ({meant[n][0]}?)" if len(meant[n]) == 1 else
                f" (one of {', '.join(meant[n][:3])}?)" if meant[n] else "") for n in gone]  # T-2000: up to three
    notes = []
    added = {}
    for f in scan(root):
        line = f"{f['file']}: {f['kind']} {f['detail']}"
        rel = os.path.normpath(f["file"])
        if rel in named and rel not in added:
            added[rel] = _added_text(root, rel)
        # Drift that predates the task is the doc's, not the task's: a ledger with old relative paths refused every
        # task that added a row to it (T-0035). Only drift in what this task wrote blocks.
        if rel in named and (added[rel] is None or f["detail"] in added[rel]):
            blockers.append(f"doc drift in a doc this task updated: {line}")
        else:
            notes.append(line)
    # T-2381: a flag or subcommand of the repo's own CLI that the lines it added name and no file has
    docs = [os.path.join(root, n) for n in sorted(named - set(gone)) if not n.startswith(("..", "/"))
            and regular(os.path.join(root, n))]
    for cl in claims(root, docs)[0] if docs else []:
        if cl["verdict"] != "failed" or cl["kind"] not in ("flag", "subcommand"):
            continue
        line, rel = f"{cl['file']}: {cl['kind']} {cl['detail']} ({cl['why']})", os.path.normpath(cl["file"])
        if rel not in added:
            added[rel] = _added_text(root, rel)
        if added[rel] is None or cl["detail"] in added[rel]:
            blockers.append(f"doc claim in a doc this task updated: {line}")
        else:
            notes.append(line)
    return blockers, notes


def cmd_docs(args):
    from sampo import cli as scli
    from sampo import core as c
    if getattr(args, "trace", False) or getattr(args, "since", None) is not None:  # T-3287: sampo.doctrace
        from sampo import doctrace
        return doctrace.cmd_trace(args)
    start = os.path.abspath(args.path or os.getcwd())
    root = c.git_root(start) or start
    findings = scan(root)
    scli.out(args, {"root": root, "findings": findings},
              "\n".join([f"{f['file']}: {f['kind']} {f['detail']}" for f in findings]) or f"No doc drift in {root}.")
    if args.strict and findings:
        raise scli.UsageError(f"{len(findings)} doc drift finding(s)")
