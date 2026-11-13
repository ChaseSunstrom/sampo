"""T-2369 (Mission 6, #5): a capability miss during an active task is that task's to resolve. The PostToolUseFailure hook
logs `capability_miss` (task, data name/kind/cmd); fm next leads with the open ones, and fm task finish (and done)
refuse the task while one is unresolved. A miss resolves only by run evidence, never by a typed line, a flag or a file
that happens to exist. Everything is read from the task's own ledger and brief, never from the global events:
  - `miss_resolved` (the PostToolUse hook logs it when a passing Bash command, in one of this task's own sessions and
    not a subagent, has the miss's key: the whole command, cd prefix and spacing aside),
  - a [ran] evidence line with exit 0 and that key, later than the miss (fm ran it; a typed line can't be one),
  - `fix_paired` (data failed, cmd: fmfixes paired a passing command with the failure, T-1032), only while the tool is
    present now, and never for a no-op command (touch, mkdir, true, :, test, [),
  - `tool_proved` (data name: a tool built and proved from runs),
  - `grant_queued` (data name, optional step: a grant asked for) plus a later passing [ran] run of that step.
  - T-4593: fm code's close logs `miss_resolved` itself (with a why) for a miss no criterion's verify and no keep check
    invokes, a tool the run tried and dropped; a miss a verify invokes is never set aside.
A run counts only when strictly later than the miss (timestamps are whole seconds). The way out short of resolving is
fm task block with the diagnosis. No miss logged on the task: nothing here runs."""
import importlib.util
import os
import re
import shutil

from sampo import core as c
from sampo import fixes

KINDS = ("capability_miss", "fix_paired", "tool_proved", "grant_queued", "miss_resolved")
CUT = 1000  # a key is the whole command up to here; the event keeps as much, so both sides cut alike
NOOPS = fixes.NOOPS  # T-2413: one list with fmfixes (they succeed with anything: pairing with one proves nothing)
_PASS = re.compile(r"^- (?:\((step|ac) (\d+)\) )?`([^`]*)` → exit 0\b")
_CD = re.compile(r"^(?:\s*cd\s+\S+\s*&&)+\s*")


def key(cmd):
    """What two runs of one command share: no leading `cd X &&` (any number), one space between words, redacted as
    the ledger keeps it."""
    return c.redact(fixes._norm(_CD.sub("", str(cmd or "").replace("`", "'"))))[:CUT]


def _data(e):
    return e.get("data") if isinstance(e.get("data"), dict) else {}


def own_module(root, name, rev=None):
    """T-4061: name is a module the project already tracks (NAME.py or NAME/__init__.py anywhere in it), in the index
    or, with rev, in that commit or tree: its import failed because of how the command ran (python3 -m unittest
    plugin.tests.test_x from the root can't see plugin/tests/helpers.py), not because a capability is missing. Only a
    file tracked before the run counts, so a stub added afterwards resolves nothing."""
    if not re.fullmatch(r"[A-Za-z_]\w*", name or ""):
        return False
    out = c._git(root, *(("ls-tree", "-r", "--name-only", rev) if rev else ("ls-files",)), timeout=30, fail="")
    return any(f.rsplit("/", 1)[-1] == f"{name}.py" or f == f"{name}/__init__.py" or f.endswith(f"/{name}/__init__.py")
               for f in out.splitlines())


def _present(p, name, kind):
    """Is the missing tool there now: a command on PATH, a Python module findable (top-level only: nothing imports),
    a node module under the project's node_modules."""
    try:
        if kind == "module":
            return importlib.util.find_spec(name.split(".")[0]) is not None
        if kind == "node":
            return os.path.isdir(os.path.join(p.root, "node_modules", name))
        return shutil.which(name) is not None
    except (ImportError, ValueError):
        return False


def _noop(cmd):
    toks = key(cmd).split()
    return bool(toks) and os.path.basename(toks[0]) in NOOPS


def open_misses(p, b):
    """[{name, cmd, kind, key, ts}], the newest miss of each command logged on this task that nothing has resolved."""
    evs = [e for e in c.ledger_events(p, KINDS, since=str(b.meta.get("created") or "")) if e.get("task") == b.id]
    latest = {}
    for i, e in enumerate(evs):
        d = _data(e)
        if e.get("event") == "capability_miss" and d.get("name"):
            latest[(d["name"], key(d.get("cmd")))] = (i, e)
    if not latest:
        return []
    passes = []  # (ts, step or None, key) of each passing run fm recorded: a [ran] line, which a typed line can't be
    for line in b.evidence():
        m = _PASS.match(line)
        t = c._TS_TAIL.search(line)
        if m and t and c._RAN_MARK in line:
            passes.append((t.group(1), int(m.group(2)) if m.group(1) == "step" else None, key(m.group(3))))
    out, base = [], None
    for (name, k), (i, miss) in sorted(latest.items(), key=lambda kv: kv[1][0]):
        ts, d, done, kind = str(miss.get("ts") or ""), _data(miss), False, _data(miss).get("kind") or "command"
        if kind == "module":  # T-4061: tracked at the task's start, so the run was wrong, not the project
            base = base or c.task_base(p.root, b) or ""
            if base and own_module(p.root, name, base):
                continue
        for e in evs[i + 1:]:
            x, ev = _data(e), e.get("event")
            if ev == "miss_resolved":
                done = done or x.get("name") == name and x.get("key") == k
            elif ev == "tool_proved":
                done = done or x.get("name") == name
            elif ev == "fix_paired":
                done = done or (key(x.get("failed"))[:300] == k[:300] and not _noop(x.get("cmd"))
                                and _present(p, name, kind))
            elif ev == "grant_queued" and x.get("name") == name:
                gts = str(e.get("ts") or "")
                done = done or any(t > gts and s is not None and (not x.get("step") or s == int(x["step"]))
                                   for t, s, _ in passes)
        done = done or any(t > ts and pk == k for t, _, pk in passes)
        if not done:
            out.append({"name": name, "cmd": d.get("cmd") or "", "kind": kind, "key": k, "ts": ts})
    return out


def on_pass(p, b, sid, cmd):
    """The PostToolUse hook, a passing Bash command on the active task (never a subagent's): when it is an open miss's
    command and this session has ledger events on the task, log `miss_resolved` so readers need only the ledger."""
    hit = [m for m in open_misses(p, b) if m["key"] == key(cmd)]
    if not hit or sid not in {e.get("session_id") for e in c.ledger_tail(p, 1500) if e.get("task") == b.id}:
        return
    for m in hit:
        c.log_event(p, "miss_resolved", task=b.id, session=sid, data={"name": m["name"], "key": m["key"]})


def gaps(p, b):
    """One line per open miss, for the refusal fm task finish and fm task done give. `fm task finish --run CMD` is
    refused before it runs, so the way to run the command green is fm task evidence."""
    return [f"capability miss: `{m['name']}` (ran `{c.defang(c.plain(m['cmd']))}`) is unresolved; find it "
            f"(fm which \"{m['name']}\"), or build and test it (fm new tool NAME), then run the command green with "
            f"fm task evidence {b.id} --step N --run '<the command>' (or queue the grant, fm ask {b.id} plugin, with a "
            f"fallback step that passes); only a later passing run closes it, no flag, typed line or file does; "
            f"fm task block {b.id} \"<why>\" is the way out" for m in open_misses(p, b)]


def safe_gaps(p, b):
    """gaps for the close gates: a reader that breaks (a torn ledger line, a full disk) must not stop a close, so it
    fails open and logs the error where the hooks' errors go (fm doctor reads it)."""
    try:
        return gaps(p, b)
    except Exception:
        import traceback
        try:
            path = os.path.join(c.state_dir(), "logs", "hooks.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            c.append_line(path, f"{c.now()} fmmiss.gaps {c.redact(traceback.format_exc()).rstrip()}\n")
        except OSError:
            pass
        return []


def absent(root, err):
    """T-4593: the tool a failed command's output says is not here (the hook's own reading of it, a project's own module
    aside), or ''. The name is untrusted text: a plain tool name or nothing."""
    from sampo import hooks
    m = hooks._MISSING.search(str(err or "")[-4000:])
    name = next((x for x in m.groups() if x), "") if m else ""
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", name):
        return ""
    return "" if m.lastindex == 3 and own_module(root, name) else name


def invoked(name, *texts):
    """T-4593: whether any of these command texts names the tool as a whole word (a longer word or a hyphenated one is
    another word). Text only: the conservative reading, so a doubtful one keeps the miss open."""
    pat = r"(?<![\w.+-])" + re.escape(name) + r"(?![\w+-])"
    return any(re.search(pat, t or "") for t in texts)


def set_aside(p, b, sid, texts, why):
    """T-4593: fm code's close sets aside the open misses whose tool none of these commands (the criteria's verifies,
    the keep check) invokes: a tool the run tried and went on without needs nothing resolved. Logged as `miss_resolved`
    with the reason, the gate's own record, so the audit trail shows it. The names set aside, once each."""
    names = []
    for m in open_misses(p, b):
        if invoked(m["name"], *texts):
            continue
        c.log_event(p, "miss_resolved", task=b.id, session=sid, data={"name": m["name"], "key": m["key"], "why": why})
        if m["name"] not in names:
            names.append(m["name"])
    return names


def lead(p, b):
    """The open miss fm next leads with, or ''."""
    ms = open_misses(p, b)
    if not ms:
        return ""
    m = ms[0]
    return (f"{b.id} has an open miss `{m['name']}`: fm which \"{m['name']}\" (find) or fm new tool NAME (build, test), "
            f"then run it green; it can't finish while open" + (f" (+{len(ms) - 1} more)" if len(ms) > 1 else ""))
