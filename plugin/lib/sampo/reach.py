"""T-5197 (child c1 of T-4311, Other machines within a minute): what a change needs from a running session.

needs(root, a, b) sorts the files that differ between commits a and b into what a running session must do:
  now      the next call takes it (plugin/lib, the hook dispatcher script plugin/hooks/hook: each event runs it fresh)
  reload   the session re-reads its hook table (plugin/hooks/hooks.json)
  restart  a monitor is started again (plugin/monitors/monitors.json), and so is any plugin file no row names
  read     the session reads the rules again (plugin/rules/)
Tests, docs (docs/, MASTER.md, plugin/README.md) and anything outside plugin/ that no row names need nothing.
PATTERNS is the one path-pattern table, data not code: a path takes the bucket of the first row that names it. A row
ending in "/" names every path under that folder; any other row names exactly one file.

code_id(root, ref) names the plugin code at a commit: a hash of the blob ids and paths of every plugin/ file except
plugin/tests/, read from the commit (not the worktree), so it moves on a committed plugin change and on nothing else.
Pure git reads; no state is written and no file is touched."""
import hashlib

from sampo import core as c

NOW, RELOAD, RESTART, READ, NONE = "now", "reload", "restart", "read", None
BUCKETS = ("now", "reload", "restart", "read")

PATTERNS = (
    ("plugin/tests/", NONE),
    ("plugin/README.md", NONE),
    ("plugin/lib/", NOW),
    ("plugin/hooks/hooks.json", RELOAD),
    ("plugin/hooks/", NOW),
    ("plugin/monitors/monitors.json", RESTART),
    ("plugin/rules/", READ),
    ("plugin/", RESTART),  # any other plugin file: restarting is the answer that never leaves stale code running
    ("docs/", NONE),
    ("MASTER.md", NONE),
)


def bucket_of(rel):
    """The bucket of one repo-relative path, None when a running session needs nothing for it."""
    for row, bucket in PATTERNS:
        if rel == row or row.endswith("/") and rel.startswith(row):
            return bucket
    return NONE


def _git(root, *args):
    """git's stdout through core's one git runner (T-0961); a failure raises here, as this module's callers expect."""
    r = c._git(root, *args, timeout=60, proc=True)
    if r.returncode:
        raise ValueError(f"git {args[0]} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def _ref(name):
    """A ref name only: one starting with a dash would be read by git as an option (e.g. --output=PATH)."""
    if not isinstance(name, str) or not name or name.startswith("-"):
        raise ValueError(f"not a ref: {name!r}")
    return name


def needs(root, a, b):
    """{bucket: sorted paths} for every bucket, all four always present (empty lists when nothing is needed)."""
    out = {k: [] for k in BUCKETS}
    for rel in filter(None, _git(root, "diff", "--name-only", "--no-renames", "-z", _ref(a), _ref(b), "--").split("\0")):
        bucket = bucket_of(rel)
        if bucket:
            out[bucket].append(rel)
    return {k: sorted(v) for k, v in out.items()}


def code_id(root, ref="HEAD"):
    """12 hex digits naming the plugin code at ref (tests left out): equal ids mean equal plugin code."""
    rows = [row for row in _git(root, "ls-tree", "-r", "-z", _ref(ref), "--", "plugin").split("\0")
            if row and not row.split("\t", 1)[1].startswith("plugin/tests/")]
    return hashlib.sha1("\0".join(rows).encode("utf-8")).hexdigest()[:12]
