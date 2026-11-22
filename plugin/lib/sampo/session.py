"""fm session (T-0323): coding-agent sessions on this device, for any supported agent, started detached so they outlive
the caller (the desktop app, an ssh pipe). A runner per session runs each turn through the agent's headless streaming
mode (claude -p stream-json, codex exec --json, gemini -p stream-json (legacy: Antigravity `agy` replaced it for individual
users on 2026-06-18), opencode run --format json, agy -p, cursor-agent -p stream-json), resumes by the
agent's own session id, and normalises its lines into one event stream:

  state/agent-sessions/<id>/meta.json      agent, cwd, status (starting|running|idle|stopped|died), turns, ids
  state/agent-sessions/<id>/events.jsonl   {ts, turn, kind: user|init|text|tool|tool_result|result|error|status|raw, …}
  state/agent-sessions/<id>/raw.log        the agent's own stdout and stderr
  state/agent-sessions/<id>/pending/       messages waiting for the next turn

One runner at a time holds run.lock; `send` queues a message and starts a runner, which exits at once if another holds
the lock (that one picks the message up before it lets go). The claude normaliser is checked against a real session;
codex, gemini, opencode, agy and cursor follow their docs (none was installed to run here), and lines they add later stay "raw"."""
import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

from sampo import core as c
from sampo import killsafe

AGENTS = ("claude", "codex", "gemini", "opencode", "agy", "cursor")  # gemini is legacy (T-3347): Code Assist Enterprise/API keys only
BINARY = {"cursor": "cursor-agent"}  # the executable where it differs from the agent's name
FM_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(__file__)))), "bin", "fm")
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
_NESTED = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "FOREMAN_SESSION_ID", "FOREMAN_DRIVE_TASK")


def root():
    return os.path.join(c.state_dir(), "agent-sessions")


def _dir(sid):
    if not _ID.fullmatch(str(sid or "")):  # ids arrive from the CLI and the app (maybe over ssh): never a path
        raise ValueError(f"not a session id: {sid}")
    return os.path.join(root(), sid)


def read_meta(sid):
    try:
        with open(os.path.join(_dir(sid), "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise ValueError(f"no session {sid}")


def write_meta(sid, meta):
    c.write_atomic(os.path.join(_dir(sid), "meta.json"), json.dumps(meta))


def _set(sid, **kw):
    meta = read_meta(sid)
    meta.update(kw, updated=c.now())
    write_meta(sid, meta)
    return meta


def _emit(sid, turn, ev):
    row = {"ts": c.now(), "turn": turn, **{k: v for k, v in ev.items() if v is not None}}
    c.append_line(os.path.join(_dir(sid), "events.jsonl"), json.dumps(row) + "\n")


# ---------------------------------------------------------------- agents: argv and normalisers

def argv(agent, prompt, resume, model, extra):
    c.refuse_if_paused()  # T-0591: whatever path got here
    prompt = " " + prompt if prompt.startswith("-") else prompt  # never read as an option
    resume = resume if resume and not str(resume).startswith("-") else None  # nor a resume id
    if agent == "claude":
        return (["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
                + (["--resume", resume] if resume else []) + (["--model", model] if model else []) + list(extra))
    if agent == "codex":  # --full-auto: codex's own sandbox (workspace writes, no network); exec is read-only without it
        return (["codex", "exec", "--json", "--full-auto"] + (["--model", model] if model else []) + list(extra)
                + (["resume", resume] if resume else []) + [prompt])
    if agent == "gemini":
        return (["gemini", "-p", prompt, "--output-format", "stream-json"] + (["--resume", resume] if resume else [])
                + (["--model", model] if model else []) + list(extra))
    if agent == "opencode":
        return (["opencode", "run", "--format", "json"] + (["--session", resume] if resume else [])
                + (["--model", model] if model else []) + list(extra) + [prompt])
    if agent == "cursor":  # T-3347: cursor-agent; auth is CURSOR_API_KEY. No --force here: like claude's permission flags, the caller grants it in `extra`
        return (["cursor-agent", "-p", prompt, "--output-format", "stream-json"]
                + (["--resume", resume] if resume else []) + (["--model", model] if model else []) + list(extra))
    if agent == "agy":  # T-3347: Antigravity CLI (docs only); the resume flag is a guess until a real install shows it
        return (["agy", "-p", prompt, "--output-format", "stream-json"] + (["--resume", resume] if resume else [])
                + (["--model", model] if model else []) + list(extra))
    raise ValueError(f"unknown agent {agent} (one of {', '.join(AGENTS)})")


def _detail(x):
    if isinstance(x, dict):
        for k in ("command", "file_path", "path", "filePath", "pattern", "query", "url", "description"):
            if isinstance(x.get(k), str) and x[k]:
                return x[k][:300]
        return json.dumps(x, ensure_ascii=False)[:300]
    return str(x or "")[:300]


def _text(x):
    if isinstance(x, list):
        return "\n".join(_text(b) for b in x)
    if isinstance(x, dict):
        return str(x.get("text") or x.get("content") or "")
    return str(x or "")


def _ev(kind, **kw):
    return dict(kind=kind, **kw)


def normalise(agent, obj, st):
    """One agent output line (a parsed JSON object; None at the end of the turn) as events. `st` is per-turn state.
    A line of a shape the normaliser doesn't expect is kept raw: it never stops the runner."""
    try:
        return {"claude": _claude, "codex": _codex, "gemini": _gemini, "opencode": _opencode,
                "cursor": _cursor, "agy": _agy}[agent](obj, st)
    except (AttributeError, TypeError, KeyError, ValueError):
        return _raw(obj) if obj is not None else []


def _raw(obj):
    return [_ev("raw", text=json.dumps(obj, ensure_ascii=False)[:500])]


def _claude(o, st):
    if o is None:
        return []
    t = o.get("type")
    if t == "system":  # hook runs, panes, thinking-token counts: claude's own housekeeping
        return [_ev("init", agent_session=o.get("session_id"), model=o.get("model"))] if o.get("subtype") == "init" else []
    if t == "rate_limit_event":
        return []
    if t in ("assistant", "user"):
        out = []
        for b in (o.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and t == "assistant":
                out.append(_ev("text", text=b.get("text") or ""))
            elif b.get("type") == "tool_use":
                out.append(_ev("tool", tool=b.get("name"), detail=_detail(b.get("input")), id=b.get("id")))
            elif b.get("type") == "tool_result":
                out.append(_ev("tool_result", id=b.get("tool_use_id"), ok=not b.get("is_error"),
                               text=_text(b.get("content"))[-2000:]))
        return out
    if t == "result":
        return [_ev("result", ok=not o.get("is_error") and o.get("subtype", "success") == "success",
                    text=str(o.get("result") or "")[:4000], cost_usd=o.get("total_cost_usd"),
                    agent_session=o.get("session_id"))]
    return _raw(o)


def _codex(o, st):
    if o is None:
        return []
    t, item = o.get("type"), o.get("item") or {}
    it, iid = item.get("type"), item.get("id")
    started = st.setdefault("started", set())
    if t == "thread.started":
        return [_ev("init", agent_session=o.get("thread_id"))]
    if t in ("turn.started", "item.updated"):
        return []
    if t == "turn.completed":
        return [_ev("result", ok=True, usage=o.get("usage"))]
    if t == "turn.failed":
        return [_ev("result", ok=False, text=str((o.get("error") or {}).get("message") or "turn failed"))]
    if t == "error":
        return [_ev("error", text=str(o.get("message") or ""))]

    def call():
        if it == "command_execution":
            return _ev("tool", tool="Bash", detail=str(item.get("command") or "")[:300], id=iid)
        if it == "file_change":
            return _ev("tool", tool="Edit", id=iid,
                       detail=", ".join(str(ch.get("path")) for ch in item.get("changes") or [] if isinstance(ch, dict))[:300])  # not a list: a detail cut at 300 characters
        if it == "mcp_tool_call":
            return _ev("tool", tool=f"{item.get('server')}.{item.get('tool')}", detail=_detail(item.get("arguments")), id=iid)
        if it == "web_search":
            return _ev("tool", tool="WebSearch", detail=str(item.get("query") or "")[:300], id=iid)
        return None
    if t == "item.started":
        ev = call()
        if ev:
            started.add(iid)
        return [ev] if ev else []
    if t == "item.completed":
        if it == "agent_message":
            return [_ev("text", text=str(item.get("text") or ""))]
        if it == "error":
            return [_ev("error", text=str(item.get("message") or ""))]
        ev = call()
        if not ev:
            return []
        ok = item.get("exit_code") == 0 if it == "command_execution" else item.get("status") == "completed"
        res = _ev("tool_result", id=iid, ok=ok, text=str(item.get("aggregated_output") or "")[-2000:])
        return [res] if iid in started else [ev, res]
    return _raw(o)


def _gemini(o, st):
    def flush():
        buf, st["buf"] = st.get("buf"), ""
        return [_ev("text", text=buf)] if buf else []
    if o is None:
        return flush()
    t = o.get("type")
    if t == "init":
        return [_ev("init", agent_session=o.get("session_id"), model=o.get("model"))]
    if t == "message":
        if o.get("role") != "assistant":
            return []
        if o.get("delta"):
            st["buf"] = (st.get("buf") or "") + str(o.get("content") or "")
            return []
        return flush() + [_ev("text", text=str(o.get("content") or ""))]
    if t == "tool_use":
        return flush() + [_ev("tool", tool=o.get("tool_name"), detail=_detail(o.get("parameters")), id=o.get("tool_id"))]
    if t == "tool_result":
        err = o.get("error")
        return [_ev("tool_result", id=o.get("tool_id"), ok=o.get("status") == "success",
                    text=str(o.get("output") or (err.get("message") if isinstance(err, dict) else err) or "")[-2000:])]
    if t == "error":
        return flush() + [_ev("error", text=str(o.get("message") or ""))]
    if t == "result":
        return flush() + [_ev("result", ok=o.get("status") == "success")]
    return _raw(o)


def _cursor(o, st):  # cursor-agent stream-json; shapes from its docs, unverified against a real binary
    if o is None:
        return []
    t = o.get("type")
    if t == "system":
        return [_ev("init", agent_session=o.get("session_id"), model=o.get("model"))] if o.get("subtype") == "init" else []
    if t == "user":
        return []
    if t == "assistant":
        return [_ev("text", text=b.get("text") or "") for b in (o.get("message") or {}).get("content") or []
                if isinstance(b, dict) and b.get("type") == "text"]
    if t == "tool_call":
        name, body = next(iter((o.get("tool_call") or {}).items()), (None, {}))  # {"readToolCall": {"args", "result"}}
        if o.get("subtype") == "started":
            return [_ev("tool", tool=re.sub(r"ToolCall$", "", name or ""), detail=_detail(body.get("args")), id=o.get("call_id"))]
        res = body.get("result") or {}
        return [_ev("tool_result", id=o.get("call_id"), ok="success" in res,
                    text=_text(res.get("success") or res.get("error") or "")[-2000:])]
    if t == "result":
        return [_ev("result", ok=not o.get("is_error") and o.get("subtype", "success") == "success",
                    text=str(o.get("result") or "")[:4000], agent_session=o.get("session_id"))]
    return _raw(o)


def _agy(o, st):  # agy's JSON envelope {conversation_id, status, response, usage}; per-event types are not documented: raw
    if o is None:
        return []
    if "status" in o and "conversation_id" in o:
        return [_ev("init", agent_session=o.get("conversation_id")), _ev("text", text=str(o.get("response") or "")),
                _ev("result", ok=o.get("status") == "success", usage=o.get("usage"))]
    return _raw(o)


def _opencode(o, st):
    if o is None:
        return []
    out = []
    if o.get("sessionID") and not st.get("sid"):
        st["sid"] = o["sessionID"]
        out.append(_ev("init", agent_session=o["sessionID"]))
    t, part = o.get("type"), o.get("part") or {}
    if t == "text":
        return out + [_ev("text", text=str(part.get("text") or ""))]
    if t == "tool_use":
        state = part.get("state") or {}
        out.append(_ev("tool", tool=part.get("tool"), detail=_detail(state.get("input")), id=part.get("callID")))
        if state.get("status") in ("completed", "error"):
            out.append(_ev("tool_result", id=part.get("callID"), ok=state.get("status") == "completed",
                           text=str(state.get("output") or state.get("error") or "")[-2000:]))
        return out
    if t == "error":
        e = o.get("error") or {}
        return out + [_ev("error", text=str((e.get("data") or {}).get("message") or e.get("message") or e.get("name")
                                            or "error"))]
    if t in ("step_start", "step_finish"):
        return out
    return out + _raw(o)


# ---------------------------------------------------------------- the runner

def _lock(f):
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _pending(sid):
    d = os.path.join(_dir(sid), "pending")
    try:
        return sorted(n for n in os.listdir(d) if n.endswith(".txt"))
    except FileNotFoundError:
        return []


def _take(sid):
    for name in _pending(sid):
        path = os.path.join(_dir(sid), "pending", name)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            os.remove(path)
            return text
        except OSError:
            continue
    return None


def _queue(sid, text):
    d = os.path.join(_dir(sid), "pending")
    os.makedirs(d, exist_ok=True)
    c.write_atomic(os.path.join(d, f"{time.time_ns()}-{os.getpid()}.txt"), text)


def _child_env():
    return {k: v for k, v in os.environ.items() if k not in _NESTED}


def _spawn(sid, cwd):
    with open(os.path.join(_dir(sid), "raw.log"), "a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, FM_BIN, "session", "_run", sid], cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, close_fds=True, env=_child_env())


def run(sid):
    """The detached runner: one turn per queued message until none is left."""
    lock = open(os.path.join(_dir(sid), "run.lock"), "a")
    if not _lock(lock):
        return 0  # another runner has it, and takes this message before it lets go
    while True:
        if os.path.exists(os.path.join(_dir(sid), "stopped")):
            return 0  # fm session stop came first; a send clears the marker
        prompt = _take(sid)
        if prompt is None:
            _set(sid, status="idle", pid=None)
            fcntl.flock(lock, fcntl.LOCK_UN)
            if _pending(sid) and _lock(lock):  # a message that arrived while we were letting go
                continue
            return 0
        _turn(sid, prompt)


def _turn(sid, prompt):
    n = read_meta(sid)["turns"] + 1
    meta = _set(sid, status="running", pid=os.getpid(), turns=n)
    if os.path.exists(os.path.join(_dir(sid), "stopped")):  # a stop that raced the take
        _set(sid, status="stopped", pid=None)
        return
    agent = meta["agent"]
    _emit(sid, n, _ev("user", text=prompt))
    with open(os.path.join(_dir(sid), "raw.log"), "a", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.Popen(argv(agent, prompt, meta.get("agent_session"), meta.get("model"), meta.get("extra") or []),
                                    cwd=meta["cwd"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                                    encoding="utf-8", errors="replace", env=_child_env())
        except OSError as e:
            _emit(sid, n, _ev("error", text=f"can't start {agent}: {e}"))
            _emit(sid, n, _ev("result", ok=False, text=f"{agent} is not installed or not on PATH"))
            return
        st, got = {}, False
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                try:
                    obj = json.loads(line)
                    evs = normalise(agent, obj, st) if isinstance(obj, dict) else _raw(obj)
                except ValueError:
                    evs = [_ev("raw", text=line.strip()[:500])] if line.strip() else []
                for ev in evs:
                    if ev.get("agent_session") and ev["agent_session"] != meta.get("agent_session"):
                        meta = _set(sid, agent_session=ev["agent_session"])
                    got = got or ev["kind"] == "result"
                    _emit(sid, n, ev)
            for ev in normalise(agent, None, st):
                _emit(sid, n, ev)
        except BaseException:
            proc.kill()  # the runner is going down: never leave the agent writing to a pipe nobody reads
            raise
        rc = proc.wait()
    if not got:
        _emit(sid, n, _ev("result", ok=rc == 0, text=f"{agent} exited {rc}" + (": " + _tail_log(sid) if rc else "")))


def _tail_log(sid, n=300):
    try:
        with open(os.path.join(_dir(sid), "raw.log"), encoding="utf-8", errors="replace") as f:
            return " ".join(f.read()[-n:].split())
    except OSError:
        return ""


# ---------------------------------------------------------------- commands

def _alive(pid, sid):
    """The session's runner is alive: pid answers, and (where /proc exists) it is this session's runner, not a reuse."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return sid.encode() in f.read()
    except OSError:  # no /proc (macOS): ask ps, and never claim a pid we can't place
        try:
            r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
            return sid in r.stdout
        except (OSError, subprocess.SubprocessError):
            return False


def start(agent, cwd, prompt, model=None, extra=(), title=None, resume=None):
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent} (one of {', '.join(AGENTS)})")
    cwd = os.path.realpath(cwd or os.getcwd())
    if not os.path.isdir(cwd):
        raise ValueError(f"no folder {cwd}")
    if not (prompt or "").strip():
        raise ValueError("a session starts with a message")
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    os.makedirs(_dir(sid))
    p = c.find_project(cwd)
    meta = {"v": 1, "id": sid, "agent": agent, "cwd": cwd, "project": p.slug if p else None,
            "title": c.fit(title or prompt.strip().splitlines()[0], 80), "model": model, "extra": list(extra),
            "created": c.now(), "updated": c.now(), "status": "starting", "turns": 0, "pid": None,
            "agent_session": resume if resume and not str(resume).startswith("-") else None}  # T-0328: continue one
    write_meta(sid, meta)
    _queue(sid, prompt)
    _spawn(sid, cwd)
    return meta


def send(sid, text):
    meta = read_meta(sid)
    if not (text or "").strip():
        raise ValueError("nothing to send")
    try:
        os.remove(os.path.join(_dir(sid), "stopped"))  # a message resumes a stopped session
    except FileNotFoundError:
        pass
    _queue(sid, text)
    _spawn(sid, meta["cwd"])  # the runner owns meta.json from here: no write that could lose its agent_session
    return meta


def stop(sid):
    # T-1789: the marker first, then the pid. A runner sets its pid before it looks for the marker, so it either sees
    # the marker and ends its turn, or set its pid before this reads it and is killed below; read first, a runner
    # starting in between was neither, ran on, and its writes put the status back to running.
    c.write_atomic(os.path.join(_dir(sid), "stopped"), c.now())  # a runner that hasn't started its turn yet sees this
    meta = read_meta(sid)
    for name in _pending(sid):
        try:
            os.remove(os.path.join(_dir(sid), "pending", name))
        except OSError:
            pass
    pid = meta.get("pid")
    if _alive(pid, sid):
        try:
            killsafe.killpg(pid, signal.SIGTERM)  # the runner leads its own group: the agent and its tools go with it
            end = time.time() + 3
            while time.time() < end and _group(pid):
                time.sleep(0.05)
            if _group(pid):
                killsafe.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    meta = _set(sid, status="stopped", pid=None)
    _emit(sid, meta["turns"], _ev("status", text="stopped"))
    return meta


def _group(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _status(meta):
    if meta.get("status") in ("starting", "running") and not _alive(meta.get("pid"), meta.get("id", "")):
        age = c.age_days(meta.get("updated")) or 0
        return meta["status"] if meta["status"] == "starting" and age * 86400 < 10 else "died"
    return meta.get("status")


def sessions():
    out = []
    try:
        names = os.listdir(root())
    except FileNotFoundError:
        return out
    for sid in names:
        try:
            meta = read_meta(sid)
        except ValueError:
            continue
        meta["status"] = _status(meta)
        last = c.tail_jsonl(os.path.join(_dir(sid), "events.jsonl"), 1)
        if last:
            meta["last"] = {k: last[-1].get(k) for k in ("ts", "kind", "tool", "ok")} | {
                "text": c.fit(str(last[-1].get("text") or last[-1].get("detail") or ""), 160)}
        out.append(meta)
    return sorted(out, key=lambda m: m.get("updated") or "", reverse=True)


def _watch_paths(_v):
    paths = [root()]
    for sid in sorted(os.listdir(root())) if os.path.isdir(root()) else []:
        d = os.path.join(root(), sid)
        paths += [os.path.join(d, "meta.json"), os.path.join(d, "events.jsonl")]
    return paths


def remove(sid):
    meta = read_meta(sid)
    if _alive(meta.get("pid"), sid):
        raise ValueError(f"session {sid} is running: stop it first")
    shutil.rmtree(_dir(sid))


def agents():
    out = []
    for a in AGENTS:
        path = shutil.which(BINARY.get(a, a))
        version = None
        if path:
            try:
                r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
                version = (r.stdout or r.stderr).strip().splitlines()[0][:80] if (r.stdout or r.stderr).strip() else None
            except (OSError, subprocess.SubprocessError):
                pass
        out.append({"agent": a, "installed": bool(path), "path": path, "version": version})
    return out


def tail(sid, start=0, follow=False, interval=0.3):
    path = os.path.join(_dir(sid), "events.jsonl")
    read_meta(sid)
    n, buf = 0, ""
    try:
        with open(path, "a+", encoding="utf-8") as f:
            f.seek(0)
            while True:
                chunk = f.read()
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for line in lines:
                        if n >= start and line.strip():
                            print(line, flush=follow)
                        n += 1
                if not follow:
                    return
                time.sleep(interval)
    except FileNotFoundError:
        raise ValueError(f"session {sid} was removed")
    except (BrokenPipeError, KeyboardInterrupt):
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass


def cmd_session(args):
    from sampo import cli
    a, rest = args.action, list(args.rest or [])

    def need(k):
        if len(rest) < k:
            raise cli.UsageError(f"fm session {a} needs {'an id' if k == 1 else 'an id and a message'}")
        return rest
    try:
        if a == "_run":
            return run(need(1)[0])
        if a in ("start", "send"):
            c.refuse_if_paused()  # T-0591
        if a == "start":
            if not rest:
                raise cli.UsageError("fm session start needs a message")
            m = start(args.agent, args.cwd, " ".join(rest), args.model, args.arg, args.title)
            return cli.out(args, m, f"{m['id']} started: {m['agent']} in {m['cwd']}")
        if a == "send":
            sid, *text = need(2)
            m = send(sid, " ".join(text))
            return cli.out(args, m, f"{sid}: sent")
        if a == "stop":
            m = stop(need(1)[0])
            return cli.out(args, m, f"{m['id']} stopped")
        if a == "rm":
            remove(need(1)[0])
            return cli.out(args, {"removed": rest[0]}, f"{rest[0]} removed")
        if a == "tail":
            return tail(need(1)[0], args.from_line, args.follow, args.interval)
        if a == "agents":
            rows = agents()
            return cli.out(args, {"v": 1, "agents": rows}, "\n".join(
                f"{r['agent']:9} {r['version'] or ('installed' if r['installed'] else '-')}" for r in rows))
        if args.follow:  # the desktop app's live list: a new line whenever a session starts, moves or ends
            from sampo import watch
            return watch._follow(lambda: {"v": 1, "sessions": sessions()}, _watch_paths, args.interval)
        rows = sessions()
        return cli.out(args, {"v": 1, "sessions": rows}, "\n".join(
            f"{r['id']}  {r['agent']:8} {r['status']:8} {r['title']}  ({r['cwd']})" for r in rows) or "no sessions")
    except ValueError as e:
        raise cli.UsageError(str(e))
