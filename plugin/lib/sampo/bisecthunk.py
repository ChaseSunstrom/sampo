"""T-3269: sampo bisect --hunks: which hunk of the first bad commit breaks the check. Each hunk of that commit (against its
first parent) is reverted alone in a throwaway worktree of the commit, and the check run: a hunk whose revert makes the
check pass is the culprit, named by file and line range. The user's tree and worktrees are left as they were."""
import contextlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from sampo import core as c

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree: the parent of a root commit
MAX_HUNKS = 60  # ponytail: one check run per hunk; a bigger commit tests its first 60 and says so. Upgrade: group by file


def run_group(argv, cwd, timeout, capture=False):
    """(exit code, stdout) of argv in cwd, or (None, "") past timeout seconds. The run gets a process group of its own and
    the whole group is killed when it ends, so a check's children (`a & b`, a server it starts) die with it (T-3269 review).
    Python's .pyc caches are off: a same-size edit inside one second kept the other version's bytecode."""
    p = subprocess.Popen(argv, cwd=cwd, text=True, stdin=subprocess.DEVNULL, start_new_session=True,
                         stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                         stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        return None, ""
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # the group outlives its leader while a child runs; ESRCH when it's gone
        except OSError:
            pass


def _passes(cmd, cwd, timeout):
    """The check's verdict in cwd: exit 0 within timeout seconds (a timeout counts as failing)."""
    return run_group(["sh", "-c", cmd], cwd, timeout)[0] == 0


@contextlib.contextmanager
def scratch_worktree(root, sha, prefix):
    """Yields a detached worktree of sha in a temp dir, or None when git can't make one. On exit it removes its own
    worktree and its own admin record under <common-dir>/worktrees, and nothing else: a whole-repo `worktree prune` would
    drop the stale records other lanes rely on (T-3269 review)."""
    admin = None
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as t:
            wt = os.path.join(t, "wt")
            try:
                if c._git(root, "worktree", "add", "--detach", "-q", wt, sha, timeout=120, fail=None) is None \
                        or not os.path.isdir(wt):
                    yield None
                    return
                git_dir = c._git(wt, "rev-parse", "--git-dir", timeout=10, fail="").strip()
                admin = os.path.realpath(os.path.join(wt, git_dir))
                yield wt
            finally:
                c._git(root, "worktree", "remove", "--force", wt, timeout=60, fail=None)
    finally:
        # remove failed and left the record: take it, but only a record under worktrees/ (never the main .git)
        if admin and os.path.basename(os.path.dirname(admin)) == "worktrees" and os.path.isdir(admin):
            shutil.rmtree(admin, ignore_errors=True)


def _ours(wt):
    """wt is its own top level, so the reset and clean below touch only the throwaway tree."""
    top = c._git(wt, "rev-parse", "--show-toplevel", timeout=10, fail="").strip()
    return bool(top) and os.path.realpath(top) == os.path.realpath(wt)


def _reset(wt, sha):
    """wt as the commit sha has it, files and index, with nothing the last check left behind. False if git can't."""
    return (c._git(wt, "reset", "--hard", "-q", sha, timeout=60, fail=None) is not None
            and c._git(wt, "clean", "-fdqx", timeout=60, fail=None) is not None)


def _lines(hunk):
    """The new file's lines a -U0 hunk covers: "2", "2-4", or "after 2" for a pure deletion."""
    m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", hunk)
    if not m:
        return "?"
    start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
    if count == 0:
        return f"after {start}"
    return str(start) if count == 1 else f"{start}-{start + count - 1}"


def culprit_hunks(root, sha, cmd, timeout):
    """The hunks of sha whose revert alone makes cmd pass: {"hunks": [{"file", "lines", "text"}], "total", "skipped",
    "tested", "unreverted", "passes_at_sha", "fails_at_parent", "error"}. total counts the diff's hunks; skipped are those
    of files git C-quotes (a tab, quote or backslash in the name), which aren't parsed and so aren't tested. passes_at_sha:
    the check passes on sha itself (a flaky check), so nothing is named. fails_at_parent: the check fails on the parent
    too, so sha alone isn't the cause. error: why nothing could be tested."""
    from sampo import cli  # lazy: cli's bisect verb imports this module
    res = {"hunks": [], "total": 0, "skipped": 0, "tested": 0, "unreverted": 0, "passes_at_sha": False,
           "fails_at_parent": False, "error": ""}
    parent = c._git(root, "rev-parse", "--verify", "-q", f"{sha}^1", timeout=10).strip() or EMPTY_TREE
    diff = c._git(root, "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "--no-renames", "-U0",
                  parent, sha, timeout=120, fail=None)
    if diff is None:
        res["error"] = f"git can't diff {sha[:12]} against its parent, so no hunk was tested"
        return res
    hunks = cli._hunks(diff)
    res["total"] = sum(1 for x in diff.splitlines() if x.startswith("@@ "))
    res["skipped"] = res["total"] - len(hunks)
    if not hunks:
        return res
    deadline = time.monotonic() + max(60, timeout * 20)  # the whole pass, budgeted as first_bad budgets its bisect
    with scratch_worktree(root, sha, "fm-culprit-") as wt:
        if wt is None or not _ours(wt):
            res["error"] = "no throwaway worktree of that commit could be made, so no hunk was tested"
            return res
        if _passes(cmd, wt, timeout):
            res["passes_at_sha"] = True
            return res
        for f, head, h in hunks[:MAX_HUNKS]:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if not _reset(wt, sha):
                res["error"] = "git reset failed in the throwaway worktree, so the rest of the hunks weren't tested"
                break
            res["tested"] += 1
            if cli._revert_hunk(os.path.join(wt, f), head, h):  # its lines moved: not testable alone
                res["unreverted"] += 1
                continue
            if _passes(cmd, wt, max(1, min(timeout, left))):
                res["hunks"].append({"file": f, "lines": _lines(h), "text": c.fit(c.plain(" ".join(
                    x[1:].strip() for x in h.splitlines()[1:] if x[:1] in "+-")), 120)})
        if not res["hunks"] and parent != EMPTY_TREE and _reset(wt, parent):  # a root commit has no parent to check
            res["fails_at_parent"] = not _passes(cmd, wt, timeout)
    return res


def report(found):
    """The lines `sampo bisect --hunks` prints under the first bad commit."""
    if found.get("error"):
        return f"  {found['error']}"
    if found["passes_at_sha"]:
        return "  the check passes on that commit when run again: a flaky check names no hunk"
    if not found["total"]:
        return "  no text hunks to revert (a binary or mode-only change)"
    if found["hunks"]:
        lines = [f"  culprit hunk: {h['file']}:{h['lines']}  {h['text']}" for h in found["hunks"]]
    elif found["tested"]:
        lines = ["  no single hunk is named: reverting any one hunk alone still fails"]
    else:
        lines = ["  no hunk was tested"]
    if found["fails_at_parent"]:
        lines.append("  the check also fails on the commit's parent: the commit alone isn't the cause")
    if found["skipped"]:
        lines.append(f"  {found['skipped']} hunk(s) not tested: git quotes their file names (a tab, quote or backslash)")
    testable = found["total"] - found["skipped"]
    if found["tested"] < testable:
        lines.append(f"  tested {found['tested']} of {testable} hunks (the cap of {MAX_HUNKS}, the time budget or an error)")
    if found["unreverted"]:
        lines.append(f"  {found['unreverted']} hunk(s) didn't revert alone: their lines moved")
    return "\n".join(lines)
