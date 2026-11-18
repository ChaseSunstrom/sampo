"""T-2372 (Mission 6, #5): a tool built during a task (fm new tool, fm mcp new) proves itself before the task closes.
Both log `tool_built` on the active task (data name, need, kind, path, test). fm task finish and fm task done then
refuse the task until, for each tool, from the task's own ledger events and evidence (a typed line replaces none):
  - search: a `tool_search` receipt (fm which, on this task) for the tool's need, earlier in the ledger than the build,
  - red then green: the tool's test run by fm ([ran] evidence: the test's path in executable position), failing and
    later passing, and the newest run passing,
  - stub: in a scratch copy the test passes on the real tool and fails with the tool's body replaced by nothing (a
    hang is killed and counts as a failure, but not on the real tool: no verdict then),
  - use: a [ran] evidence line, strictly after the green run, exit 0, whose last command runs the tool (its path in
    executable position: `runs`), not --help and not its own test. Naming the file (cat, git add, py_compile) is no run.
All four hold: `tool_proved` {name, path, need} (fmmiss reads it as a resolved miss). A task that built nothing pays one
filtered ledger read. A broken ledger read fails open, like fmmiss; a tool that can't be checked (a file that can't be
read, a scratch run that can't start) is refused by name, never skipped.
ponytail: the scratch copy holds the tool and its test only (a stdlib script and its test: what the scaffolds make);
a test that needs a sibling file fails there with the real tool and is refused with that said. `runs` reads one
command: a newline, `||`, a trailing `&`, or a tool that isn't the last command is not a use; the first non-option
argument of an interpreter is the script (`python3 -W error x.py` isn't read: run `python3 x.py`)."""
import os
import re
import shlex
import sys
import tempfile
import shutil

from sampo import core as c

KINDS = ("tool_built", "tool_search", "tool_proved")
SECONDS = 30  # each scratch run (real, then stub); a hang is killed at this
_SEP = {"&&", "||", ";", ";;", "|", "&", "|&"}
_REDIR = {">", ">>", "<", ">&", "&>", "<&", "<<", "&>>", ">|"}
_INTERP = re.compile(r"(python[\d.]*|py|node|nodejs|bash|sh|zsh|ruby|perl|deno|bun)")
_PYTEST = {"pytest", "py.test"}
_NOT_A_RUN = {"--help", "-h", "--version", "-V"}


def built(name, tool, test, kind="tool"):
    """fm new tool / fm mcp new: log the build on the active task, if any (nothing to prove outside one)."""
    try:
        p = c.find_project(os.getcwd(), create=False)
        b = p and c.active_brief_fast(p, p.lane)
        if b:
            c.log_event(p, "tool_built", task=b.id, data={"name": name, "need": name, "kind": kind,
                                                          "path": tool, "test": test})
    except Exception:
        pass  # a ledger that can't be written must not undo a scaffold that was


def _data(e):
    return e.get("data") if isinstance(e.get("data"), dict) else {}


def _argv(cmd):
    """The argv of the LAST command of `cmd` (variable assignments, env and redirections dropped), or None when the
    command can't vouch for what its exit code means: a newline, an unparsable quote, `||`, a trailing `&`, or
    nothing to run."""
    if "\n" in cmd:
        return None
    try:
        toks = list(shlex.shlex(cmd, posix=True, punctuation_chars=True))
    except ValueError:
        return None
    if "||" in toks:
        return None
    last = []
    for t in toks:
        if t in _SEP:
            last = []
        else:
            last.append(t)
    out, i = [], 0
    while i < len(last):
        if last[i] in _REDIR:
            i += 2
        else:
            out.append(last[i])
            i += 1
    while out and (re.match(r"[A-Za-z_]\w*=", out[0]) or out[0] == "env"):
        out.pop(0)
    return out or None


def _same(tok, path, root):
    try:
        return os.path.realpath(tok if os.path.isabs(tok) else os.path.join(root, tok)) == os.path.realpath(path)
    except (ValueError, OSError):
        return False


def _script(argv):
    """What the command runs: argv[0], or an interpreter's first non-option argument (None for -c and -m)."""
    if not _INTERP.fullmatch(os.path.basename(argv[0])):
        return argv[0]
    for x in argv[1:]:
        if not x.startswith("-"):
            return x
        if re.fullmatch(r"-[A-Za-z]*[cm]", x):
            return None
    return None


def runs(cmd, path, root):
    """True when the last command of `cmd` runs `path`: it is argv[0] or the script of an interpreter, and the
    arguments hold no --help. A mention (cat, git add, py_compile, echo, grep) is not a run."""
    argv = _argv(cmd)
    s = argv and _script(argv)
    return bool(s and _same(s, path, root) and not _NOT_A_RUN & set(argv[1:]))


def runs_test(cmd, test, root):
    """True when the last command of `cmd` runs the test file: as `runs` reads it, or as an argument of
    `-m unittest`, `-m pytest` or `pytest` (a `::case` suffix is fine)."""
    if runs(cmd, test, root):
        return True
    argv = _argv(cmd)
    if not argv:
        return False
    if _INTERP.fullmatch(os.path.basename(argv[0])):
        if "-m" not in argv or argv[argv.index("-m") + 1:][:1] not in (["unittest"], ["pytest"]):
            return False
        args = argv[argv.index("-m") + 2:]
    elif os.path.basename(argv[0]) in _PYTEST:
        args = argv[1:]
    else:
        return False
    return any(not a.startswith("-") and _same(a.split("::")[0], test, root) for a in args)


def _env(_folder):
    """The user's environment less credentials (the names fm bench strips): a tool that needs HOME, USER, LANG or
    PATH finds them; the real run and the stub run get the same."""
    from sampo import bench
    return {k: v for k, v in os.environ.items() if not bench.CREDENTIALS.match(k)}


def _run(tool, test, body, seconds):
    """Why the test failed in a scratch copy (None: it passed): the tool (or `body` instead) and the test, in their
    own relative layout, run through the sandbox fm new check uses for a panel."""
    from sampo import playbooks
    base = os.path.commonpath([os.path.dirname(tool), os.path.dirname(test)])
    with tempfile.TemporaryDirectory(prefix="fm-toolproof-") as d:
        for src, text in ((tool, body), (test, None)):
            dst = os.path.join(d, os.path.relpath(src, base))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if text is None:
                shutil.copyfile(src, dst)
            else:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(text)
        return playbooks._run(os.path.join(d, os.path.relpath(test, base)), seconds, playbooks._limits(),
                                env=_env, what="the test", argv=(sys.executable, "-I"))


def stub_problem(tool, test, seconds=SECONDS):
    """None when the test passes on the real tool and fails on a stub of it (its body replaced by nothing). An
    unreadable file raises: the caller names the tool that could not be checked."""
    if not (os.path.isfile(tool) and os.path.isfile(test)):
        return "stub check: the tool or its test is gone from where it was built"
    with open(tool, encoding="utf-8", errors="replace") as f:
        why = _run(tool, test, f.read(), seconds)
    if why:
        return (f"stub check: the test fails in a scratch copy even with the real tool ({why}): nothing to compare "
                f"(the copy holds only the tool and its test and runs with your environment minus credentials: names "
                f"like *_TOKEN, *_KEY, GITHUB_*; a test that needs a secret, a config or a sibling file fails there "
                f"for that environment reason)")
    if _run(tool, test, "# stubbed by fm: the body is gone\n", seconds) is None:
        return "stub check: the test still passes on a stub of the tool (its body replaced by nothing): it asserts nothing about the tool"
    return None


def _runs(b):
    """fm's own runs on the brief, oldest first: (command, passed, timestamp), from the [ran] evidence lines."""
    out = []
    for line in b.evidence():
        if c._RAN_MARK not in line or line.count("`") < 2:
            continue
        cmd = line.split("`", 2)[1]
        ts = c._TS_TAIL.search(line)
        if "` → ✗ exit" in line or "` → red as expected" in line:
            out.append((cmd, False, ""))
        elif "` → exit 0" in line:
            out.append((cmd, True, ts.group(1) if ts else ""))
    return out


def _red_green(runs_, test, root):
    """(ok, ts of the newest green): fm's runs of the test file: a failure, later a pass of that command, and the
    newest run a pass."""
    mine = [r for r in runs_ if runs_test(r[0], test, root)]
    failed, ok = set(), False
    for cmd, passed, _ in mine:
        if not passed:
            failed.add(cmd)
        elif cmd in failed:
            ok = True
    return (ok and mine[-1][1], mine[-1][2]) if mine else (False, "")


def _used(runs_, tool, root, after):
    """A passing fm run of the tool, strictly after the green run (whole-second timestamps)."""
    return any(ok and ts > after and runs(cmd, tool, root) for cmd, ok, ts in runs_)


def _one(p, b, i, d, mine, runs_):
    """The unproven pieces of one tool, as refusal lines (empty: proved). May raise: the caller refuses by name."""
    from sampo import which
    path, name, test = d["path"], d.get("name") or os.path.basename(d["path"]), d.get("test") or ""
    need, tag = set(which.words(re.sub(r"[_-]", " ", d.get("need") or name))), f"tool {name}"
    found = [f"{tag}: no search receipt for it before the build: fm which \"{name}\" must come first, and a search "
             f"now can't stand in for it (delete the tool's files and build again after searching, or fm task "
             f"block {b.id} \"<why>\")"] if not any(
        ev == "tool_search" and j < i and need & set(which.words(x.get("need") or "")) for j, _, ev, x in mine) else []
    ok, green = _red_green(runs_, test, p.root)
    if not ok:
        found.append(f"{tag}: no red then green run of its test by fm: run {os.path.basename(test)} failing, then "
                     f"passing, with fm task evidence {b.id} --step N --run 'python3 {test}' (a typed line is not a run)")
        return found
    why = stub_problem(path, test)
    if why:
        found.append(f"{tag}: {why}")
    if not _used(runs_, path, p.root, green):
        rel = os.path.relpath(path, p.root) if path.startswith(p.root + os.sep) else path
        found.append(f"{tag}: never run for real after its green run: fm task evidence {b.id} --step N --run "
                     f"\"python3 {rel} <a sample input>\" (the tool's path in executable position, exit 0, last in the "
                     f"command; cat, git add, py_compile, --help and its own test don't count; a server: pipe it one "
                     f"request)")
    return found


def gaps(p, b, log=True):
    """One line per unproven piece of each tool this task built, for the refusals of fm task finish and done.
    `log` False (T-4147, finish --dry): read only; a passing proof is not recorded as a tool_proved line."""
    evs = c.ledger_events(p, KINDS, since=str(b.meta.get("created") or ""))
    mine = [(i, e, e.get("event"), _data(e)) for i, e in enumerate(evs) if e.get("task") == b.id]
    builds = {}
    for i, e, ev, d in mine:
        if ev == "tool_built" and d.get("path"):
            builds[d["path"]] = (i, d)
    out, runs_ = [], (_runs(b) if builds else [])
    for path, (i, d) in builds.items():
        if any(ev == "tool_proved" and x.get("path") == path and j > i for j, _, ev, x in mine):
            continue
        try:
            found = _one(p, b, i, d, mine, runs_)
        except Exception as e:  # chmod 000, a path gone mid-check: refused by name, never skipped
            found = [f"tool {d.get('name') or os.path.basename(path)}: could not be checked ({e.__class__.__name__}): "
                     f"make {path} and its test readable and finish again"]
        if found:
            out += found
        elif log:
            c.log_event(p, "tool_proved", task=b.id, data={"name": d.get("name"), "path": path,
                                                           "need": d.get("need") or d.get("name")})
    return out


def safe_gaps(p, b, log=True):
    """gaps for the close gates; a ledger read that breaks must not stop a close: it fails open and logs where the
    hooks' errors go (fm doctor reads it). A tool that can't be checked is no such failure: `gaps` refuses it."""
    try:
        return gaps(p, b, log)
    except Exception:
        import traceback
        try:
            from sampo import hooks  # T-3431: rotation and repeat collapse; lazy, the failure path only
            hooks.log_error("fmtoolproof.gaps", traceback.format_exc())
        except Exception:
            pass
        return []
