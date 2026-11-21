"""Sampo on other coding agents (T-0324). Codex, Gemini CLI and opencode run the hook Claude Code runs, as
`hook --agent X EVENT`: their payload is read into Claude Code's shape, fmhooks decides exactly as it does for Claude,
and the answer goes back in the agent's own format. `fm agents install X` wires those hooks, the `fm mcp` server and an
instructions block into the agent's user config; `uninstall` takes out exactly what install put in."""
import io
import json
import os
import re
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout

from sampo import core as c

PLUGIN = os.path.dirname(os.path.dirname(os.path.realpath(os.path.dirname(__file__))))
HOOK = os.path.join(PLUGIN, "hooks", "hook")
FM = os.path.join(PLUGIN, "bin", "fm")
NAMES = ("codex", "gemini", "opencode")
TOOL_EVENTS = {"PreToolUse", "BeforeTool"}

# The events each agent's config wires in, by its own name → Claude Code's. Gemini's AfterAgent would be Stop, but its
# deny throws the answer away and retries, so Foreman's drive and done-gate stay Claude Code and Codex only.
EVENTS = {
    "codex": {e: e for e in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")},
    "gemini": {"SessionStart": "SessionStart", "BeforeAgent": "UserPromptSubmit", "BeforeTool": "PreToolUse",
               "AfterTool": "PostToolUse"},
    "opencode": {e: e for e in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")},
}
SHELL = {"bash", "shell", "shell_command", "local_shell", "exec_command", "run_shell_command"}
WRITE = {"write", "write_file"}
EDIT = {"edit", "replace", "str_replace"}
PATCH = {"apply_patch", "patch"}
_PATCH_FILE = re.compile(r"^\*\*\* (?:(Add)|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


# ---- the hook adapter ----

def _path(ti, cwd):
    p = str(ti.get("file_path") or ti.get("filePath") or ti.get("path") or "")
    return os.path.join(cwd, p) if p and not os.path.isabs(p) else p


def _calls(agent, pl, cwd):
    """The agent's tool call as Claude Code tool calls [(tool_name, tool_input, cwd)]: a shell command is Bash, a
    write or edit is Write/Edit with file_path, a patch is one Write or Edit per file it touches."""
    name, ti = (pl.get("tool"), pl.get("args")) if agent == "opencode" else (pl.get("tool_name"), pl.get("tool_input"))
    name, ti = str(name or ""), ti if isinstance(ti, dict) else {}
    n = name.lower()
    if n in SHELL:
        cmd = ti.get("command", ti.get("cmd", ""))
        cmd = shlex.join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)
        wd = ti.get("workdir") or ti.get("dir_path") or ti.get("cwd")
        return [("Bash", {"command": cmd}, os.path.join(cwd, str(wd)) if wd else cwd)]
    if n in WRITE:
        return [("Write", {"file_path": _path(ti, cwd), "content": str(ti.get("content", ""))}, cwd)]
    if n in EDIT:
        return [("Edit", {"file_path": _path(ti, cwd), "old_string": str(ti.get("old_string", ti.get("oldString", ""))),
                          "new_string": str(ti.get("new_string", ti.get("newString", "")))}, cwd)]
    if n == "multiedit":
        return [("MultiEdit", {"file_path": _path(ti, cwd), "edits": ti.get("edits") or []}, cwd)]
    if n in PATCH:
        raw = next((ti[k] for k in ("command", "patchText", "patch", "input") if ti.get(k)), "")
        text = "\n".join(map(str, raw)) if isinstance(raw, list) else str(raw)
        files = [(("Write" if m.group(1) else "Edit"), m.group(2) or m.group(3)) for m in _PATCH_FILE.finditer(text)]
        # a patch whose files can't be read is judged as a write into the folder it runs in, never let through unread
        return [(t, {"file_path": _path({"path": f}, cwd), "content": text, "old_string": "", "new_string": text}, cwd)
                for t, f in files or [("Write", ".")]]
    if isinstance(ti.get("command", ti.get("cmd")), (str, list)):  # a shell tool by a name not seen yet
        return _calls(agent, {"tool": "bash", "args": ti, "tool_name": "bash", "tool_input": ti}, cwd)
    return [(name, ti, cwd)]  # not a tool the guard looks at: fmhooks lets it through


def _run(event, payload):
    from sampo import hooks as shooks
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = shooks.run(event, json.dumps(payload))
    return code, out.getvalue(), err.getvalue()


def _parse(out):
    text = out.strip()
    for cand in (text, text.splitlines()[-1] if text else ""):
        try:
            o = json.loads(cand)
            if isinstance(o, dict):
                return o, None
        except ValueError:
            pass
    return {}, text or None  # plain text is context


def hook(agent, event, raw):
    try:
        return _hook(agent, event, raw)
    except Exception as e:  # an adapter crash is Python's exit 1, which Codex and Gemini let through: refuse instead
        if event not in TOOL_EVENTS:
            return 0
        try:
            from sampo import hooks as shooks
            shooks.log_error(event, f"fm agents ({agent}): {e!r}")
        except Exception:
            pass
        return _deny(agent, f"Ward internal error ({type(e).__name__}); the tool call was blocked "
                            f"(fail-closed)")


def _hook(agent, event, raw):
    tool = event in TOOL_EVENTS
    if agent not in EVENTS:
        print(f"Sampo: no hook adapter for agent {agent!r} (known: {', '.join(NAMES)}); "
              f"{'the tool call was refused' if tool else 'nothing ran'}", file=sys.stderr)
        return 2 if tool else 0
    ev = EVENTS[agent].get(event)
    try:
        pl = json.loads(raw) if raw.strip() else {}
    except ValueError:
        pl = None
    if ev is None or not isinstance(pl, dict):
        if tool:
            print("Sampo: unreadable tool event; the tool call was refused (fail-closed)", file=sys.stderr)
        return 2 if tool else 0
    cwd = str(pl.get("cwd") or os.getcwd())
    # Claude Code's transcript fields are left out: another agent's transcript isn't in Claude Code's format
    base = {k: pl[k] for k in ("session_id", "prompt", "source", "tool_response") if k in pl}
    if agent == "opencode" and "output" in pl:
        base["tool_response"] = {"stdout": str(pl["output"] or "")}
    if ev == "PostToolUse":  # T-2378: no host has a failure event, so an after-tool event whose output shows a missing
        said = _said(base.get("tool_response"))  # tool is Claude Code's PostToolUseFailure: the same miss and note
        if _MISS_HINT.search(said):
            from sampo import hooks as shooks  # only past the cheap check: most after-tool events never load it
            if shooks._MISSING.search(said[-4000:]):
                ev, base["error"] = "PostToolUseFailure", said[-4000:]
    base.update(hook_event_name=ev, cwd=cwd)
    calls = _calls(agent, pl, cwd) if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure") else [None]
    for call in calls:
        payload = dict(base)
        if call:
            payload.update(tool_name=call[0], tool_input=call[1], cwd=call[2])
        code, out, err = _run(ev, payload)
        o, text = _parse(out)
        hs = o.get("hookSpecificOutput") or {}
        if ev == "PreToolUse" and (code == 2 or hs.get("permissionDecision") in ("deny", "ask")):
            reason = hs.get("permissionDecisionReason") or err.strip() or "Sampo's guard refused this tool call"
            if hs.get("permissionDecision") == "ask":
                reason += (f" ({agent} can't show Sampo's permission prompt: grant this from a Claude Code session "
                           f"in this project)")
            return _deny(agent, reason)
    ctx = hs.get("additionalContext") or text
    stop = o.get("reason") if o.get("decision") == "block" else (err.strip() if code == 2 and ev == "Stop" else None)
    return _answer(agent, event, ev, ctx, stop, code, err)


_MISS_HINT = re.compile(r"not found|No module named|Cannot find module")


def _said(r):
    """A host's tool response as text: a string, or a dict's stdout, stderr, output and error."""
    if isinstance(r, str):
        return r
    return "\n".join(str(r[k]) for k in ("stdout", "stderr", "output", "error") if isinstance(r.get(k), str)) \
        if isinstance(r, dict) else ""


def _deny(agent, reason):
    if agent == "codex":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    else:
        print(json.dumps({"decision": "deny", "reason": reason} if agent == "gemini" else {"deny": reason}))
    print(reason, file=sys.stderr)
    return 2


def _answer(agent, event, ev, ctx, stop, code, err):
    if agent == "opencode":
        if ctx:
            print(json.dumps({"context": ctx}))
        return 0
    if stop:  # Codex only (see EVENTS): a block is "keep going, here's why"
        print(json.dumps({"decision": "block", "reason": stop}))
        return 0
    if ctx and not (agent == "gemini" and ev == "PreToolUse"):  # Gemini reads no context before a tool runs
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}}))
    if code == 2:
        print(err.strip(), file=sys.stderr)
        return 2
    return 0


# ---- fm agents install / uninstall / list ----

PLUGIN_MARK = "written by `fm agents install opencode`"
MD_START, MD_END = "<!-- >>> sampo (fm agents install; fm agents uninstall removes it) -->", "<!-- <<< sampo -->"
TOML_START, TOML_END = "# >>> sampo (fm agents install codex; fm agents uninstall codex removes it)", "# <<< sampo"


def _older(s):
    """T-2946: the marker an install wrote before the rename (it said foreman); read and removed, never written."""
    return s.replace(">>> sampo", ">>> foreman").replace("<<< sampo", "<<< foreman")


def _config_dirs():
    home = os.path.expanduser("~")
    return {"codex": os.environ.get("CODEX_HOME") or os.path.join(home, ".codex"),
            "gemini": os.path.join(home, ".gemini"),
            "opencode": os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "opencode")}


def _files(agent):
    d = _config_dirs()[agent]
    if agent == "codex":
        return {"hooks": os.path.join(d, "hooks.json"), "mcp": os.path.join(d, "config.toml"),
                "rules": os.path.join(d, "AGENTS.md")}
    if agent == "gemini":
        s = os.path.join(d, "settings.json")
        return {"hooks": s, "mcp": s, "rules": os.path.join(d, "GEMINI.md")}
    return {"plugin": os.path.join(d, "plugins", "foreman.ts"), "mcp": os.path.join(d, "opencode.json"),
            "rules": os.path.join(d, "AGENTS.md")}


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _load_json(path):
    text = _read(path)
    if text is None or not text.strip():
        return {}
    try:
        o = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{path} isn't plain JSON ({e.msg}, line {e.lineno}); left it alone — add Sampo to it by "
                         f"hand or remove the comments first")
    if not isinstance(o, dict):
        raise ValueError(f"{path} isn't a JSON object; left it alone")
    return o


def _save(path, text):
    """Write, or remove a file that's now empty (one install created holds nothing of the user's)."""
    if not text.strip() or text.strip() == "{}":
        if os.path.exists(path):
            os.remove(path)
        return
    path = os.path.realpath(path)  # a dotfile symlinked from a dotfiles repo stays a symlink
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c.write_atomic(path, text)


def _hook_cmd(agent, event):
    return f"{shlex.quote(HOOK)} --agent {agent} {event}"


def _ours(agent, h):
    cmd = h.get("command") if isinstance(h, dict) else None
    return isinstance(cmd, str) and "hooks/hook" in cmd and f"--agent {agent} " in cmd


def _strip_hooks(cfg, agent):
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return
    for ev in list(hooks):
        groups = []
        for g in hooks[ev] if isinstance(hooks[ev], list) else []:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                keep = [h for h in g["hooks"] if not _ours(agent, h)]
                if not keep:
                    continue
                g = dict(g, hooks=keep)
            groups.append(g)
        if groups or not isinstance(hooks[ev], list):
            hooks[ev] = groups if isinstance(hooks[ev], list) else hooks[ev]
        else:
            del hooks[ev]
    if not hooks:
        del cfg["hooks"]


def _add_hooks(cfg, agent):
    hooks = cfg.setdefault("hooks", {})
    for ev in EVENTS[agent]:
        h = {"type": "command", "command": _hook_cmd(agent, ev)}
        if agent == "gemini":
            h.update(name="foreman", timeout=30000)
        else:
            h["timeout"] = 30
        hooks.setdefault(ev, []).append({"hooks": [h]})  # no matcher: every tool goes past the guard


def _block(text, start, end, body):
    """text with Sampo's marked block removed (body None) or replaced/appended (body str)."""
    # the separator install added goes with the block; text the user put after it keeps a line break before it
    for s, e in ((start, end), (_older(start), _older(end))):
        text = re.sub(r"(\n?)" + re.escape(s) + r".*?" + re.escape(e) + r"\n?",
                      lambda m: "\n" if m.group(1) and m.end() < len(m.string) else "", text or "", flags=re.S)
    if body is None:
        return text
    sep = "" if not text else "\n" if text.endswith("\n") else "\n\n"
    return f"{text}{sep}{start}\n{body}\n{end}\n"


def rules_text():
    return (f"## Sampo\n"
            f"Sampo keeps the work here disciplined. Its CLI is `fm` ({FM}); the `fm` MCP server has the same state.\n"
            f"- No brief, edits refused: `fm task new \"<t>\" --type T --tier S|M --ac \"<done> :: <cmd>\" "
            f"--step \"<s>\" --focus`; M: 2 readings, 3+ fails-if options, Choice N (NN%) (`fm outcomes --judgment`).\n"  # T-2344
            f"- Follow `fm next`: the one next required action and how to do it; what starts by itself: "
            f"`fm triggers`, `fm night --tick`.\n"  # T-2364
            f"- Nothing is done without fresh evidence (a model's answer advises, a run decides): run each verify, then "
            f"`fm task finish ID --audit \"<how>\"`; `fm prove --rollup` lists what no run proved.\n"  # T-2395
            f"- A refused command says why and how it is granted; never work around the guard. A missing tool: "
            f"`fm which X`; an install waits for the user: `fm ask ID plugin --later`.\n"  # T-2378: #5 on every host
            f"- The full rules: {c.rules_path(PLUGIN)}")  # T-5048


def _opencode_plugin():
    with open(os.path.join(PLUGIN, "integrations", "opencode", "foreman.ts"), encoding="utf-8") as f:
        return f.read().replace("__HOOK__", json.dumps(HOOK))


def apply(agent, on=True):
    """Install (on) or uninstall Sampo's wiring for one agent. Returns (changed paths, notes)."""
    if agent not in NAMES:
        raise ValueError(f"unknown agent {agent!r} (known: {', '.join(NAMES)})")
    f, notes, planned = _files(agent), [], {}
    if agent in ("codex", "gemini"):
        cfg = _load_json(f["hooks"])
        _strip_hooks(cfg, agent)
        if on:
            _add_hooks(cfg, agent)
        planned[f["hooks"]] = cfg
    if agent == "codex":
        toml = _read(f["mcp"]) or ""
        own = _block(toml, TOML_START, TOML_END, None)
        body = f"[mcp_servers.fm]\ncommand = {json.dumps(FM)}\nargs = [\"mcp\"]"
        if on and re.search(r"^\s*\[mcp_servers\.fm\]", own, re.M):
            notes.append(f"{f['mcp']} already has an [mcp_servers.fm] of its own; kept it")
            body = None
        if on and re.search(r"^\s*hooks\s*=\s*false", own, re.M):
            notes.append(f"{f['mcp']} turns hooks off ([features] hooks = false): the guard won't run until it's removed")
        planned[f["mcp"]] = _block(toml, TOML_START, TOML_END, body if on else None)
    else:
        cfg = planned.get(f["mcp"]) if f["mcp"] in planned else _load_json(f["mcp"])
        key, entry = ("mcpServers", {"command": FM, "args": ["mcp"]}) if agent == "gemini" else \
            ("mcp", {"type": "local", "command": [FM, "mcp"], "enabled": True})
        servers = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
        if on and servers.get("fm") not in (None, entry):
            notes.append(f"{f['mcp']} already has an \"fm\" MCP server of its own; kept it")
        elif on:
            cfg[key] = dict(servers, fm=entry)
        elif servers.get("fm") == entry:
            servers.pop("fm")
            if servers:
                cfg[key] = servers
            else:
                cfg.pop(key, None)
        planned[f["mcp"]] = cfg
    if agent == "opencode":
        cur = _read(f["plugin"])
        if cur is not None and PLUGIN_MARK not in cur:
            if on:
                raise ValueError(f"{f['plugin']} is a plugin of your own, not Sampo's; left it alone")
        else:
            planned[f["plugin"]] = _opencode_plugin() if on else ""
    planned[f["rules"]] = _block(_read(f["rules"]), MD_START, MD_END, rules_text() if on else None)
    changed = []
    for path, val in planned.items():  # every file read and checked before any is written
        text = json.dumps(val, indent=2, ensure_ascii=False) + "\n" if isinstance(val, dict) else val
        if text != (_read(path) or "") and not (_read(path) is None and text.strip() in ("", "{}")):
            _save(path, text)
            changed.append(path)
    return changed, notes


def status(agent):
    f, have = _files(agent), []
    if agent == "opencode":
        if os.path.exists(f["plugin"]):
            have.append("plugin")
    else:
        try:
            hooks = _load_json(f["hooks"]).get("hooks") or {}
        except ValueError:
            hooks = {}
        if any(_ours(agent, h) for gs in hooks.values() if isinstance(gs, list) for g in gs if isinstance(g, dict)
               for h in g.get("hooks") or []):
            have.append("hooks")
    if json.dumps(FM) in (_read(f["mcp"]) or ""):
        have.append("MCP")
    if any(m in (_read(f["rules"]) or "") for m in (MD_START, _older(MD_START))):
        have.append("rules")
    guard = "plugin" if agent == "opencode" else "hooks"
    return {"agent": agent, "installed": guard in have, "detail": ", ".join(have) or None,
            "config": _config_dirs()[agent]}


CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
ROLES = {"SessionStart": "context", "UserPromptSubmit": "context", "PreToolUse": "blocks (guard)",
         "PostToolUse": "records", "Stop": "keeps working (drive, done-gate)"}


def matrix():
    """T-0442: per harness, what each of Claude Code's hook events does there, from EVENTS: the guard blocks, the drive
    keeps the session working, the rest is context or records; "absent" where the harness has no such hook."""
    out = {"claude": {e: ROLES[e] for e in CLAUDE_EVENTS}}
    for a in NAMES:
        mapped = set(EVENTS[a].values())
        out[a] = {e: ROLES[e] if e in mapped else "absent" for e in CLAUDE_EVENTS}
    return out


def cmd_agents(args):
    from sampo import cli
    try:
        if args.action == "list":
            rows, m = [status(a) for a in NAMES], matrix()
            return cli.out(args, {"v": 1, "agents": rows, "matrix": m}, "\n".join(
                f"{r['agent']:9} {'wired' if r['installed'] else 'not wired':10} {r['detail'] or ''}" for r in rows)
                + "\n\nWhat each harness enforces (T-0442; an ask is a deny outside Claude Code):\n" + "\n".join(
                f"{a:9} " + "; ".join(f"{e}: {v}" for e, v in row.items()) for a, row in m.items()))
        if not args.agent:
            raise ValueError(f"fm agents {args.action} needs an agent: {', '.join(NAMES)}")
        on = args.action == "install"
        changed, notes = apply(args.agent, on)
        if on:  # say what isn't proven yet (T-0335–T-0337 wait on live CLIs)
            notes = notes + [f"built to {args.agent}'s documented hook format, not yet verified against a live "
                             f"{args.agent}: check a refused command there before relying on the guard"]
        verb = "wired into" if on else "taken out of"
        text = "\n".join([f"Sampo {verb} {args.agent}" + (f": {', '.join(changed)}" if changed else
                                                            " (nothing to change)")] + [f"note: {n}" for n in notes])
        return cli.out(args, dict(status(args.agent), changed=changed, notes=notes), text)
    except (ValueError, OSError) as e:
        raise cli.UsageError(str(e))
