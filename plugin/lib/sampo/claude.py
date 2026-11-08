"""fm claude (T-0328): every Claude Code session on this device — terminal, Remote Control and headless — read from
Claude Code's own store, so the desktop app (locally or over ssh) can list them, read their conversations (text, tool
calls, images), open their subagents, browse their scratchpads, and continue any of them.

  $CLAUDE_CONFIG_DIR (~/.claude)/projects/<dir>/<id>.jsonl                 the transcript, one entry per line
  …/projects/<dir>/<id>/subagents/agent-<aid>.jsonl + .meta.json           each subagent's transcript and type
  $TMPDIR/claude-<uid>/<dir>/<id>/scratchpad/                              the session's scratch files

The transcript format is Claude Code's internal one: entries are read defensively and anything unexpected is
skipped. Everything here reads, except `send`, which continues a session headlessly through fm session
(`claude -p --resume <id>`, with --fork-session while the session is live so two writers never share a file)."""
import base64
import calendar
import glob
import json
import os
import re
import stat
import tempfile
import time

from sampo import core as c

_SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_AID = re.compile(r"a?[0-9a-f]{6,40}")
LIVE_S = 90  # a transcript written, or a statusline drawn, this recently: the session is open
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".svg": "image/svg+xml"}
_memo = {}  # path -> ((mtime_ns, size), summary): one entry per transcript, for --follow
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _clean(s):
    """Text for a terminal: no control characters (a file named with an escape sequence stays inert)."""
    return _CTRL.sub("?", str(s))


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _transcript(sid, agent=None):
    if not _SID.fullmatch(str(sid or "")):
        raise ValueError(f"not a Claude session id: {sid}")
    hits = glob.glob(os.path.join(config_dir(), "projects", "*", f"{sid}.jsonl"))
    if not hits:
        raise ValueError(f"no Claude session {sid} on this device")
    path = hits[0]
    if agent is None:
        return path
    if not _AID.fullmatch(str(agent)):
        raise ValueError(f"not a subagent id: {agent}")
    sub = os.path.join(path[:-6], "subagents", f"agent-{agent}.jsonl")
    if not os.path.exists(sub):
        raise ValueError(f"no subagent {agent} in {sid}")
    return sub


def _entries(text):
    for line in text.split("\n"):  # never splitlines(): U+2028 and friends sit raw inside JSON strings
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            yield e


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def _prompt(e):
    """A user entry's typed text, or None for tool results, meta lines and command echoes."""
    if e.get("type") != "user" or e.get("isMeta"):
        return None
    t = _text_of((e.get("message") or {}).get("content")).strip()
    cmd = re.search(r"<command-name>([^<]+)</command-name>", t)
    if cmd:  # a slash command: "/sampo:build resume"
        args = re.search(r"<command-args>([^<]*)</command-args>", t)
        return f"{cmd.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    return None if not t or c.machine_text(t) else t  # T-3687: a user's pasted HTML is still theirs


def kind_of(entrypoint):
    ep = str(entrypoint or "")
    return "terminal" if ep == "cli" else "remote" if "remote" in ep else "headless" if ep.startswith("sdk") else ep or "?"


def _snapshot_age(sid):
    try:
        with open(os.path.join(c.state_dir(), "sessions", f"{sid}.json"), encoding="utf-8") as f:
            ts = json.load(f).get("ts")
        return time.time() - calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def summary(path):
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    if _memo.get(path, (None,))[0] == key:
        out = dict(_memo[path][1])
    else:
        head = []  # whole lines (a first prompt can be hundreds of KB), at most 300 of them or 8 MB
        with open(path, "rb") as f:
            used = 0
            for raw in f:
                head.append(raw.decode("utf-8", "replace"))
                used += len(raw)
                if len(head) >= 300 or used > 8 * 1024 * 1024:
                    break
            f.seek(max(0, st.st_size - 128 * 1024))
            tail = f.read().decode("utf-8", "replace")
        head = "".join(head)
        sid = os.path.basename(path)[:-6]
        out = {"id": sid, "dir": os.path.basename(os.path.dirname(path)), "cwd": None, "entrypoint": None,
               "branch": None, "version": None, "prompt": None, "title": None, "model": None, "last": None}
        for e in _entries(head):
            out["cwd"] = out["cwd"] or e.get("cwd")
            out["entrypoint"] = out["entrypoint"] or e.get("entrypoint")
            out["branch"] = out["branch"] or e.get("gitBranch")
            out["version"] = out["version"] or e.get("version")
            out["prompt"] = out["prompt"] or _prompt(e)
            if out["prompt"] and out["cwd"] and out["entrypoint"]:
                break
        for e in _entries(tail):
            if e.get("type") == "ai-title" and e.get("aiTitle"):
                out["title"] = e["aiTitle"]
            m = e.get("message") if isinstance(e.get("message"), dict) else {}
            if e.get("type") == "assistant":
                out["model"] = m.get("model") or out["model"]
                t = _text_of(m.get("content")).strip()
                if t:
                    out["last"] = c.fit(" ".join(t.split()), 200)
        out["prompt"] = c.fit(" ".join((out["prompt"] or "").split()), 300) or None
        out["title"] = out["title"] or c.fit(out["prompt"] or out["last"] or "(no prompt)", 80)
        out["kind"] = kind_of(out["entrypoint"])
        out["size"] = st.st_size
        _memo[path] = (key, dict(out))
    out["updated"] = st.st_mtime // 15 * 15  # coarse: a busy transcript doesn't re-send the list on every write
    snap = _snapshot_age(out["id"])
    out["live"] = time.time() - st.st_mtime < LIVE_S or (snap is not None and snap < LIVE_S)
    out["subagents"] = len(glob.glob(os.path.join(path[:-6], "subagents", "*.meta.json")))
    return out


def sessions(include_all=False, limit=150):
    """Every terminal and Remote Control session (they're the ones a person is in), newest first; with include_all,
    also the newest `limit` headless ones (SDK and claude -p runs, which can number in the thousands)."""
    paths = sorted(glob.glob(os.path.join(config_dir(), "projects", "*", "*.jsonl")), key=_mtime, reverse=True)
    out, headless = [], 0
    for path in paths:
        if not _SID.fullmatch(os.path.basename(path)[:-6]) or not os.path.getsize(path):
            continue  # not a session, or one that never wrote a line
        try:
            s = summary(path)
        except OSError:
            continue
        if not (s["prompt"] or s["last"]):
            continue  # opened and closed: nothing to show
        if s["kind"] == "headless":
            if not include_all or headless >= limit:
                continue
            headless += 1
        out.append(s)
    return out


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _detail(name, inp):
    from sampo import session
    if name in ("Agent", "Task") and isinstance(inp, dict):
        return c.fit(f"{inp.get('subagent_type') or 'agent'}: {inp.get('description') or ''}", 300)
    return session._detail(inp)


def _events(n, e):
    t, ts = e.get("type"), e.get("timestamp")
    content = (e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None
    base = {"n": n, "ts": ts}
    att = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
    if t == "attachment" and att.get("type") == "queued_command":  # typed while Claude was working
        text = att.get("prompt") if isinstance(att.get("prompt"), str) else _text_of(att.get("prompt"))
        if text and text.strip() and not c.machine_text(text):  # notifications and other sessions' messages queue too
            yield dict(base, kind="user", text=text, queued=True)
        return
    if t == "user" and not e.get("isMeta"):
        if isinstance(content, str):
            if content.strip() and not c.machine_text(content):
                yield dict(base, kind="user", text=content)
            return
        for i, b in enumerate(content if isinstance(content, list) else []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip() and not c.machine_text(b["text"]):
                yield dict(base, kind="user", text=b["text"])
            elif b.get("type") == "image":
                yield dict(base, kind="image", ref=f"{n}:{i}", media_type=(b.get("source") or {}).get("media_type"))
            elif b.get("type") == "tool_result":
                inner = b.get("content")
                for j, x in enumerate(inner if isinstance(inner, list) else []):
                    if isinstance(x, dict) and x.get("type") == "image":
                        yield dict(base, kind="image", ref=f"{n}:{i}:{j}", tool_use_id=b.get("tool_use_id"),
                                   media_type=(x.get("source") or {}).get("media_type"))
                yield dict(base, kind="tool_result", id=b.get("tool_use_id"), ok=not b.get("is_error"),
                           text=_text_of(inner)[-2000:])
    elif t == "assistant":
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                yield dict(base, kind="text", text=b["text"])
            elif b.get("type") == "tool_use":
                yield dict(base, kind="tool", tool=b.get("name"), id=b.get("id"), detail=_detail(b.get("name"), b.get("input")))


def image(path, ref):
    parts = str(ref).split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not an image ref: {ref}")
    n, idx = int(parts[0]), [int(p) for p in parts[1:]]
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i == n:
                try:
                    block = json.loads(line)["message"]["content"][idx[0]]
                    if len(idx) == 2:
                        block = block["content"][idx[1]]
                    src = block["source"]
                    if block.get("type") == "image" and src.get("type") == "base64":
                        return {"media_type": src.get("media_type"), "data": src["data"]}
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    pass
                break
    raise ValueError(f"no image at {ref}")


def subagents(path):
    out = []
    for meta in sorted(glob.glob(os.path.join(path[:-6], "subagents", "agent-*.meta.json"))):
        aid = os.path.basename(meta)[len("agent-"):-len(".meta.json")]
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            m = {}
        jl = meta[:-len(".meta.json")] + ".jsonl"
        out.append({"id": aid, "type": m.get("agentType"), "description": m.get("description"),
                    "tool_use_id": m.get("toolUseId"), "updated": _mtime(jl),
                    "live": time.time() - _mtime(jl) < LIVE_S})
    return sorted(out, key=lambda a: a["updated"], reverse=True)


def scratchpad(path):
    sid = os.path.basename(path)[:-6]
    return os.path.join(tempfile.gettempdir(), f"claude-{os.getuid()}", os.path.basename(os.path.dirname(path)), sid,
                        "scratchpad")


def files(path, cap=2000):
    root = scratchpad(path)
    out = []
    if not _own_dir(root):
        return root, out, False
    for dirpath, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            ext = os.path.splitext(name)[1].lower()
            out.append({"path": os.path.relpath(full, root), "size": st.st_size, "mtime": st.st_mtime,
                        "kind": "link" if stat.S_ISLNK(st.st_mode) else "image" if ext in IMAGE_EXT else "file"})
            if len(out) >= cap:
                return root, out, True
    return root, out, False


def _own_dir(p):
    """The scratchpad and the claude-<uid> folder above it are real folders this user owns: /tmp is shared, and a
    symlink planted there must not turn a scratchpad read into a read of somewhere else."""
    for d in (os.path.dirname(os.path.dirname(os.path.dirname(p))), p):
        try:
            st = os.lstat(d)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return False
    return True


def read_file(path, rel, text_cap=2 * 1024 * 1024, bin_cap=20 * 1024 * 1024):
    if not _own_dir(scratchpad(path)):
        raise ValueError("this session's scratchpad isn't a folder you own")
    root = os.path.realpath(scratchpad(path))
    full = os.path.realpath(os.path.join(root, rel))
    if os.path.isabs(rel) or os.path.commonpath([root, full]) != root or os.path.islink(os.path.join(root, rel)):
        raise ValueError(f"{rel} isn't a file in this session's scratchpad")
    if not os.path.isfile(full):
        raise ValueError(f"no file {rel}")
    ext = os.path.splitext(full)[1].lower()
    size = os.path.getsize(full)
    try:
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))  # swapped for a symlink since the check: no
    except OSError as e:
        raise ValueError(f"can't read {rel}: {e.strerror}")
    with os.fdopen(fd, "rb") as f:
        if ext in IMAGE_EXT:
            if size > bin_cap:
                raise ValueError(f"{rel} is too big to show ({size} bytes)")
            return {"path": rel, "media_type": IMAGE_EXT[ext], "data": base64.b64encode(f.read()).decode()}
        raw = f.read(text_cap)
    return {"path": rel, "text": raw.decode("utf-8", "replace"), "truncated": size > text_cap, "size": size}


def send(sid, message, model=None):
    from sampo import session
    s = summary(_transcript(sid))
    if not s.get("cwd") or not os.path.isdir(s["cwd"]):
        raise ValueError(f"the session's folder {s.get('cwd')} isn't on this device any more")
    # always a fork: a terminal can sit idle past any liveness window, and two writers must never share a transcript
    meta = session.start("claude", s["cwd"], message, model, ["--fork-session"], title=f"↳ {s['title']}", resume=sid)
    return dict(meta, claude_session=sid, forked=True, live=s["live"])


def cmd_claude(args):
    from sampo import cli
    a, rest = args.action, list(args.rest or [])

    def need(k, what):
        if len(rest) < k:
            raise cli.UsageError(f"fm claude {a} needs {what}")
        return rest
    try:
        if a == "list":
            build = lambda: {"v": 1, "sessions": sessions(args.all, args.limit)}  # noqa: E731
            if args.follow:
                from sampo import watch
                return watch._follow(build, lambda v: [os.path.join(config_dir(), "projects")] + [
                    os.path.join(config_dir(), "projects", s["dir"], s["id"] + ".jsonl") for s in v["sessions"][:40]],
                    args.interval)
            v = build()
            return cli.out(args, v, "\n".join(_clean(
                f"{s['id'][:8]}  {s['kind']:8} {'live ' if s['live'] else '     '}{s['title']}  ({s['cwd']})")
                for s in v["sessions"]) or "no Claude sessions")
        if a == "show":
            path = _transcript(need(1, "a session id")[0], args.agent)
            start = args.from_line if args.from_line >= 0 else max(0, _count_lines(path) + args.from_line)
            return _stream(path, start, args.follow, args.interval)
        if a == "agents":
            path = _transcript(need(1, "a session id")[0])
            rows = subagents(path)
            return cli.out(args, {"v": 1, "agents": rows}, "\n".join(
                _clean(f"{r['id']}  {r['type']}: {r['description']}") for r in rows) or "no subagents")
        if a == "image":
            sid, ref = need(2, "a session id and an image ref")[:2]
            return cli.out(args, image(_transcript(sid, args.agent), ref), None)
        if a == "files":
            root, rows, truncated = files(_transcript(need(1, "a session id")[0]))
            return cli.out(args, {"v": 1, "root": root, "files": rows, "truncated": truncated},
                             "\n".join(_clean(f"{r['size']:>9}  {r['path']}") for r in rows) or f"no files in {root}")
        if a == "file":
            sid, rel = need(2, "a session id and a path")[:2]
            return cli.out(args, read_file(_transcript(sid), rel), None)
        if a == "send":
            sid, *text = need(2, "a session id and a message")
            m = send(sid, " ".join(text), args.model)
            return cli.out(args, m, f"{m['id']}: continuing {sid}" + (" (forked: it's live)" if m["forked"] else ""))
    except ValueError as e:
        raise cli.UsageError(str(e))


def _count_lines(path):
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return n


def _stream(path, start, follow, interval):
    """Events as JSON lines from line `start`; with follow, new ones as the transcript grows (whole lines only: one
    being written is read once its newline lands)."""
    import sys
    n, buf = 0, ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            while True:
                chunk = f.read(1 << 20)  # a megabyte at a time: a transcript full of images can be huge
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for line in lines:
                        if n >= start:
                            try:
                                e = json.loads(line)
                            except ValueError:
                                e = None
                            if isinstance(e, dict):
                                try:
                                    for ev in _events(n, e):
                                        print(json.dumps(ev), flush=follow)
                                except (TypeError, AttributeError, KeyError, ValueError):
                                    pass  # an entry of a shape this reader doesn't know: skipped, never fatal
                        n += 1
                    continue  # more may already be there
                if not follow:
                    return
                time.sleep(interval)
    except (BrokenPipeError, KeyboardInterrupt):
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
