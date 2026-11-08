"""T-3276: sampo audit ahead ID prints the objections past reviews raised on this task's diff, before the review. Ranked:
the same files (cli.past_findings, the review brief's own list), then the same folders, then the defect classes
(lanes.DEFECT_CLASSES) whose cue words the diff's added lines carry, at most PER_CLASS rows each. Read-only: it writes
nothing and asks no model. Rejected findings and this task's own are left out."""
import os
import re
import subprocess

from sampo import cli
from sampo import core as c
from sampo import lanes

CAP = 8  # the author reads one short list; the review brief keeps its own longer one
PER_CLASS = 2  # one common cue word (lock, race) must not fill the list
TIMEOUT = 120  # s: a git diff of a huge change  # ponytail: no size cap on the diff, a 60 MB change reads whole


def _class_of(text):
    """The defect class a finding's text falls in: the first DEFECT_CLASSES keyword it carries (as lanes.defect_classes)."""
    low = text.lower()
    return next((n for n, ks in lanes.DEFECT_CLASSES if any(k in low for k in ks)), "other")


def _cued(added):
    """The DEFECT_CLASSES whose cue words start a word in the added lines (so "lock" cues "self.lock", not "block"),
    the class with the most cue words hit first (DEFECT_CLASSES order on a tie)."""
    low = added.lower()
    hits = {}
    for n, ks in lanes.DEFECT_CLASSES:
        n_hit = sum(1 for k in ks if re.search(r"\b" + re.escape(k), low))
        if n_hit:
            hits[n] = n_hit
    return sorted(hits, key=lambda n: -hits[n])


def likely(p, files, added, tid=None, cap=CAP):
    """[{file, text, lens, task, why}] for a diff: the same files, then the same folders, then the classes its added
    lines cue; at most cap rows, each finding once, and at most PER_CLASS per cued class."""
    rows, seen = [], set()

    def add(file, text, lens, task, why):
        text = c.fit(text, 160)
        if (file, text) in seen or len(rows) >= cap:
            return False
        seen.add((file, text))
        rows.append({"file": file, "text": text, "lens": lens, "task": task, "why": why})
        return True

    found = list(cli.past_findings(p, tid))
    want = set(files)
    for where, text, lens, task in found:
        if cli.named_by(where, want):
            add(where, text, lens, task, "same file")
    folders = {os.path.dirname(f) for f in files} - {""}  # a root file's folder is "": it is in no folder
    for where, text, lens, task in found:
        if os.path.dirname(where) in folders:
            add(where, text, lens, task, "same directory")
    for name in _cued(added):
        taken = 0
        for where, text, lens, task in found:
            if taken < PER_CLASS and _class_of(text) == name and add(where, text, lens, task,
                                                                     f"class: {name} (a cue in the diff)"):
                taken += 1
    return rows


def _pairs(p, b, rev):
    """The diffs the review runs over, as audit prep picks them: the task's start (--base, else its snapshot or start
    commit) to the working files; a lane branch's merge base to the branch, plus (T-1296, T-1579) the uncommitted edits
    here when this checkout's own task is b. Each side resolved to a tree id, as prep does."""
    base = rev or c.task_base(p.root, b)
    if not base:
        raise cli.UsageError(f"{b.id} has no start commit on record: sampo audit ahead {b.id} --base <rev>")
    tree, branch = c.worktree_tree(p.root), b.meta.get("lane_branch")
    if not tree:
        raise cli.UsageError("sampo audit ahead needs a git repository")
    pairs = [(base, tree)]
    if branch and not rev and c._git(p.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", fail=None):
        base = c._git(p.root, "merge-base", "HEAD", branch).strip() or base
        pairs = [(base, branch)]
        if getattr(c.active_brief_fast(p), "id", None) == b.id:
            pairs.append(("HEAD", tree))
    out = []
    for a, z in pairs:
        ids = []
        for x in (a, z):
            got = c._git(p.root, "rev-parse", "--verify", "-q", "--end-of-options", f"{x}^{{tree}}", fail=None)
            if not got or not got.strip():
                raise cli.UsageError(f"--base {x!r} is not a revision in this repository")
            ids.append(got.strip())
        out.append(tuple(ids))
    return out


def _diff(p, a, z):
    """The diff of changed lines only (-U0: a context line isn't an addition), with no repo-configured driver or hook."""
    try:
        r = subprocess.run(["git", "-C", p.root, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "diff",
                            "-U0", "--no-ext-diff", "--no-textconv", a, z], capture_output=True, text=True,
                           errors="replace", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise cli.UsageError(f"git diff took over {TIMEOUT} s; narrow it with --base <a later rev>")
    if r.returncode:
        raise cli.UsageError(f"git diff failed: {r.stderr.strip()[:200]}")
    return r.stdout


def ahead(p, b, args):
    """`sampo audit ahead ID`: the objections on record for this task's diff, most likely first, before the review."""
    diff = "".join(_diff(p, a, z) for a, z in _pairs(p, b, args.base))
    files = sorted(set(re.findall(r"(?m)^diff --git a/.+? b/(.+)$", diff)))
    added = "\n".join(ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    rows = likely(p, files, added, b.id)
    if rows:
        text = (f"Objections on record for {b.id} ({len(files)} files; most likely first):\n"
                + "\n".join(f"- {r['file']}: {r['text']} ({r['lens']}, {r['task']}) — {r['why']}" for r in rows))
    else:
        text = (f"none on record for {b.id}: no past finding names these files, their folders, or a defect class "
                f"the added lines cue")
    cli.out(args, {"task": b.id, "files": files, "objections": rows}, text)
