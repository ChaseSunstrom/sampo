"""T-5082 (child of T-4308): the green ref, the last tree whose hook smoke passed.

HOOK_REF is a local ref under refs/sampo/, outside refs/heads and refs/tags, so a push of the branch or of the tags
never carries it. mark(main, sha) points it at a commit the repo holds; read(main) gives that sha, or None when the
ref is unset, the commit is gone, or main is not a repo. A linked worktree shares the ref with its main checkout."""
import os
import re
import subprocess

HOOK_REF = "refs/sampo/green/hook"
TIMEOUT = 15
_SHA = re.compile(r"[0-9a-f]{4,64}")  # a hex name only: a branch, a flag or a ref path never reaches git as a revision


class RefusedSha(ValueError):
    """mark was given something that is not a commit sha this repo holds."""


def _git(main, *argv):
    """(exit code, stdout) of git argv in main; the code is 1 when git can't run or runs past TIMEOUT."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    try:
        r = subprocess.run(["git", *argv], cwd=main, capture_output=True, text=True, timeout=TIMEOUT,
                           stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return r.returncode, r.stdout.strip()


def read(main):
    """The sha HOOK_REF names in main's repo, or None."""
    code, sha = _git(main, "rev-parse", "--verify", "-q", HOOK_REF + "^{commit}")
    return sha if code == 0 and _SHA.fullmatch(sha) else None


def mark(main, sha):
    """Point HOOK_REF at the commit sha (a full sha, or a hex prefix git resolves to one commit); gives the full sha.
    Raises RefusedSha for a value that is not hex, a sha the repo lacks, an ambiguous prefix or a non-commit."""
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise RefusedSha(f"not a commit sha: {sha!r}")
    code, full = _git(main, "rev-parse", "--verify", "-q", sha + "^{commit}")
    if code or not _SHA.fullmatch(full):
        raise RefusedSha(f"this repo has no commit {sha}")
    code, _ = _git(main, "update-ref", HOOK_REF, full)
    if code:
        raise RefusedSha(f"git would not set {HOOK_REF} to {full}")
    return full
