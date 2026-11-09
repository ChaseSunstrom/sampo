"""fm install-user / uninstall-user: wire Sampo into ~/.claude and undo it exactly.

Every change is recorded with its original value in state/install-manifest.json; uninstall reverses only what
Sampo added and still owns (a value the user changed afterwards is left alone). Writes are atomic, and
~/.claude/settings.json is backed up to FOREMAN_HOME/backups before each change.
"""
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import time

from sampo import core as c

# T-3704: no rm rule. "*" in a Claude Code rule is a wildcard ("rm -rf /*" denied every rm -rf of an absolute path), and a
# deny rule's text found in a command CC can't parse simply makes it ask, bypass-immune. The Ward and CC's own
# dangerousRemoval check still stop a catastrophic delete. A backstop rule must never name a common command prefix.
DENY_RULES = [
    "Bash(mkfs *)", "Bash(mkfs.* *)", "Bash(dd * of=/dev/*)",
    "Bash(git push --force * main)", "Bash(git push -f * main)", "Bash(git push --force * master)", "Bash(git push -f * master)",
    "Edit(~/.ssh/**)", "Edit(~/.gnupg/**)", "Edit(~/.aws/credentials)", "Edit(~/.claude/.credentials.json)",
]
STOP_CAP = "60"  # drive mode: allow long queues past Claude Code's default 8 consecutive Stop-hook continuations
COMPACT_PCT = "70"  # compact earlier than the default: PreCompact checkpoints and SessionStart re-injects the task
COMPACT_WINDOW = "200k"  # T-1796: a 1M window let every reply re-read ~380k tokens; 70% of 200k compacts near 140k
ENV = {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": STOP_CAP, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": COMPACT_PCT,
       "CLAUDE_CODE_AUTO_COMPACT_WINDOW": COMPACT_WINDOW}
OUTPUT_STYLE = "sampo:Sampo"  # plugin/output-styles/foreman.md; plugin styles are selected as <plugin>:<name>
OUTPUT_STYLES = (OUTPUT_STYLE, "foreman:Sampo", "foreman:Foreman")  # T-2946, T-2879: ids installs stored before still count
WHY = {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "drive mode: long queues aren't cut at 8 continuations",
       "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "compact before long contexts degrade; Sampo state survives compaction",
       "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "every reply re-reads the whole context: a 200k window keeps it small"}
BEGIN, END = "<!-- foreman:begin -->", "<!-- foreman:end -->"
BLOCK = (f"{BEGIN}\n# Sampo\nSampo is installed. Operating rules: ~/.claude/rules/foreman.md. "
         f"System map: ~/.claude/foreman/MASTER.md.\n{END}\n")
STATUSLINE_KEEP = ("padding", "refreshInterval", "hideVimModeIndicator")


class SetupError(Exception):
    pass


def paths():
    claude = os.path.join(os.path.expanduser("~"), ".claude")
    fh = c.foreman_home()
    return {"settings": os.path.join(claude, "settings.json"), "claude_md": os.path.join(claude, "CLAUDE.md"),
            "rules_link": os.path.join(claude, "rules", "foreman.md"),
            "rules_target": os.path.join(fh, "plugin", "rules", "foreman.md"),
            "fm_link": os.path.join(os.path.expanduser("~"), ".local", "bin", "fm"),  # T-0853: fm in any shell
            "fm_target": os.path.join(fh, "plugin", "bin", "fm"),
            "wrapper": os.path.join(fh, "plugin", "hooks", "statusline"),
            "manifest": os.path.join(c.state_dir(), "install-manifest.json"), "backups": os.path.join(fh, "backups")}


def ensure_fm_link(link=None, target=None):
    """T-1333: ~/.local/bin/fm → this Sampo's bin/fm when nothing is there, or when the link there is Sampo's (its
    target a .../plugin/bin/fm with foreman in the path) and that target is gone: a plugin update removed the version it
    named. Anything else stays: a file, another program's link, a working link to another Sampo checkout. The link
    made, or None."""
    link = link or paths()["fm_link"]
    target = target or os.path.join(c.PLUGIN_ROOT, "bin", "fm")
    try:
        if os.path.islink(link):
            if os.path.exists(link) or not re.search(r"foreman.*/plugin/bin/fm$", os.readlink(link)):
                return None
            os.remove(link)
        elif os.path.lexists(link):
            return None
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(target, link)
    except OSError:  # a race, or a home this user can't write: the doctor says what's missing
        return None
    return link


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _backup(P):
    if os.path.exists(P["settings"]):
        os.makedirs(P["backups"], exist_ok=True)
        shutil.copy2(P["settings"], os.path.join(P["backups"], f"settings.json.{time.strftime('%Y%m%d-%H%M%S')}"))


def _link_state(P):
    link = P["rules_link"]
    if os.path.islink(link):
        return "ours" if os.path.realpath(link) == os.path.realpath(P["rules_target"]) else "foreign"
    return "foreign" if os.path.exists(link) else "absent"


# ---------------------------------------------------------------- install

def _model_step(new_s, new_m, actions):
    """T-3742: models.newest keeps `model` an alias and switchModelsOnFlag true; the manifest remembers what was there."""
    from sampo import modelup
    want = modelup.apply_newest(new_s)
    for key, tag in (("model", "model"), ("switchModelsOnFlag", "flag")):
        if want.get(key) != new_s.get(key):
            new_m.setdefault(tag + "_original", {"had": key in new_s, "value": new_s.get(key)})
            new_m[tag + "_set"] = want[key]
            actions.append(f"{key} = {want[key]!r} (was {new_s[key]!r})" if key in new_s else f"{key} = {want[key]!r}")
            new_s[key] = want[key]


def fix_model():
    """Re-apply models.newest to settings.json (and Codex) now; what it did, or ''. Used by `sampo models` and doctor."""
    from sampo import modelup
    P = paths()
    s = _load(P["settings"], None)
    out = []
    if s is not None:
        new_s, new_m, actions = copy.deepcopy(s), _load(P["manifest"], {}), []
        _model_step(new_s, new_m, actions)
        if new_s != s:
            c.write_atomic(P["settings"], json.dumps(new_s, indent=2) + "\n")
            if os.path.exists(P["manifest"]):
                c.write_atomic(P["manifest"], json.dumps(new_m, indent=2) + "\n")
            out += actions
    cx = modelup.codex_newest()
    if cx:
        out.append(f"codex model {cx}")
    return "; ".join(out)


def plan_install(P):
    s = _load(P["settings"], None)
    m = _load(P["manifest"], {})
    settings_created = s is None
    s = {} if s is None else s
    new_s, new_m, actions = copy.deepcopy(s), copy.deepcopy(m), []
    new_m.setdefault("settings_created", settings_created)

    sl = new_s.get("statusLine") or {}
    if sl.get("command") != P["wrapper"]:
        new_m.setdefault("statusLine_original", sl or None)
        new_s["statusLine"] = dict({k: sl[k] for k in STATUSLINE_KEEP if k in sl}, type="command", command=P["wrapper"])
        actions.append(f"statusLine → {P['wrapper']} (original saved; it still renders above the Sampo line)")
    new_m.setdefault("statusline_hud", True)

    perms_existed = "permissions" in new_s
    perms = new_s.setdefault("permissions", {})
    new_m.setdefault("permissions_created", not perms_existed)
    new_m.setdefault("defaultMode_original", perms.get("defaultMode"))
    deny_existed = "deny" in perms
    deny = perms.setdefault("deny", [])
    new_m.setdefault("deny_created", not deny_existed)
    stale = [r for r in new_m.get("deny_added", []) if r not in DENY_RULES]  # T-3704: a rule we no longer install
    if stale:
        deny[:] = [r for r in deny if r not in stale]
        new_m["deny_added"] = [r for r in new_m["deny_added"] if r not in stale]
        actions.append(f"permissions.deny -= {len(stale)} retired Sampo rules")
    added = [r for r in DENY_RULES if r not in deny]
    if added:
        deny.extend(added)
        new_m["deny_added"] = list(dict.fromkeys(new_m.get("deny_added", []) + added))
        actions.append(f"permissions.deny += {len(added)} catastrophic-command rules")
    elif not deny_existed:
        perms.pop("deny")

    env_existed = "env" in new_s
    env = new_s.setdefault("env", {})
    new_m.setdefault("env_created", not env_existed)
    for k, v in ENV.items():
        if k not in env:
            env[k] = v
            new_m.setdefault("env_added", {})[k] = v
            actions.append(f"env.{k}={v} ({WHY[k]})")
    if not env and not env_existed:
        new_s.pop("env")
    wt = new_s.get("worktree") if isinstance(new_s.get("worktree"), dict) else None
    if not (wt or {}).get("baseRef"):  # T-0803: builder lanes start from the branch being worked, not origin's default
        new_m.setdefault("worktree_created", wt is None)
        new_s["worktree"] = dict(wt or {}, baseRef="head")
        new_m["worktree_baseRef_set"] = True
        actions.append("worktree.baseRef = head (builder lanes start from your current branch, not the remote's)")
    _model_step(new_s, new_m, actions)
    if new_m.get("outputStyle_set"):  # T-1279: older installs chose the style; the rules carry the format now
        new_m.pop("outputStyle_set")
        if new_s.get("outputStyle") in OUTPUT_STYLES:  # a style the user changed to since is left alone
            new_s.pop("outputStyle")
            actions.append("outputStyle removed (the reply format now comes from the rules; saves Claude Code's per-turn style reminder)")

    md = _read(P["claude_md"])
    new_md = md
    if md is None or BEGIN not in md:
        sep = "" if not md or md.endswith("\n") else "\n"
        new_md = (md or "") + sep + BLOCK
        new_m["claude_md_block"] = True
        new_m["claude_md_sep"] = sep
        new_m.setdefault("claude_md_created", md is None)
        actions.append("~/.claude/CLAUDE.md += Sampo block (between foreman:begin/end markers)")

    link = _link_state(P)
    if link == "foreign":
        raise SetupError(f"{P['rules_link']} exists and isn't Sampo's symlink; move it aside and re-run")
    if link == "absent":
        new_m["rules_symlink"] = P["rules_link"]
        actions.append(f"{P['rules_link']} → {P['rules_target']} (symlink)")
    if not os.path.lexists(P["fm_link"]):  # T-0853: someone else's fm stays where it is
        on_path = os.path.dirname(P["fm_link"]) in os.environ.get("PATH", "").split(os.pathsep)
        actions.append(f"{P['fm_link']} → {P['fm_target']} (fm in any shell"
                       + ("" if on_path else "; add ~/.local/bin to your PATH to use it outside Claude Code") + ")")
    return s, new_s, m, new_m, md, new_md, link, actions


def cmd_install(args):
    P = paths()
    if getattr(args, "record_disabled", None):
        m = _load(P["manifest"], {})
        m["plugins_disabled"] = list(dict.fromkeys(m.get("plugins_disabled", []) + args.record_disabled))
        c.write_atomic(P["manifest"], json.dumps(m, indent=2, sort_keys=True) + "\n")
        print("Recorded disabled plugins: " + ", ".join(m["plugins_disabled"]))
        return
    try:
        s, new_s, m, new_m, md, new_md, link, actions = plan_install(P)
    except SetupError as e:
        raise _usage(str(e))
    if not args.dry_run:  # T-3727: a crash must not leave empty git objects in Sampo's own repo
        from sampo import py
        py.ensure_fsync(c.foreman_home())
    if not actions:
        print("Sampo is already wired into ~/.claude; nothing to do.")
        return
    if args.dry_run:
        print("Would change:\n  - " + "\n  - ".join(actions))
        return
    _backup(P)
    os.makedirs(os.path.dirname(P["settings"]), exist_ok=True)
    if new_s != s or new_m.get("settings_created"):
        c.write_atomic(P["settings"], json.dumps(new_s, indent=2) + "\n")
    if new_md != md:
        c.write_atomic(P["claude_md"], new_md)
    if link == "absent":
        os.makedirs(os.path.dirname(P["rules_link"]), exist_ok=True)
        os.symlink(P["rules_target"], P["rules_link"])
    if not os.path.lexists(P["fm_link"]):
        os.makedirs(os.path.dirname(P["fm_link"]), exist_ok=True)
        os.symlink(P["fm_target"], P["fm_link"])
    new_m.setdefault("installed", c.now())
    c.write_atomic(P["manifest"], json.dumps(new_m, indent=2, sort_keys=True) + "\n")
    print("Wired Sampo into ~/.claude:\n  - " + "\n  - ".join(actions) +
          f"\nBackup of settings.json in {P['backups']}. Undo with: fm uninstall-user")


# ---------------------------------------------------------------- uninstall

def _compact_cmd():
    return os.path.join(c.PLUGIN_ROOT, "hooks", "compact")  # the running plugin's copy, wherever it lives


def _compact_mode(h):
    """"on", "shadow" (T-0963) or None for one hook entry of settings.json."""
    cmd = h.get("command") if isinstance(h, dict) else None
    return "on" if cmd == _compact_cmd() else "shadow" if cmd == shlex.quote(_compact_cmd()) + " --shadow" else None


def _shadow_total():
    """T-0963: what shadow mode's records add up to, by command family."""
    fams = {}
    for r in c.tail_jsonl(os.path.join(c.state_dir(), "outputs", "shadow.jsonl"), 100000):
        if not isinstance(r, dict):
            continue
        n = r.get("saved_chars") if isinstance(r.get("saved_chars"), int) else 0
        fams[str(r.get("cmd_family"))] = fams.get(str(r.get("cmd_family")), 0) + n
    top = sorted(fams.items(), key=lambda kv: -kv[1])[:3]
    return (f"would have cut {sum(fams.values()):,} characters so far" +
            (" — " + ", ".join(f"{k} {v:,}" for k, v in top) if top else ""))


def _without_compact(settings):
    """settings without Sampo's compact-output hook (and any hooks objects it leaves empty); True when it was there."""
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    groups = hooks.get("PostToolUse") or []
    keep = [g for g in groups if not any(_compact_mode(h) for h in (g.get("hooks") or []))]
    if len(keep) == len(groups):
        return False
    if keep:
        hooks["PostToolUse"] = keep
    else:
        hooks.pop("PostToolUse", None)
    if not hooks:
        settings.pop("hooks", None)
    return True


def cmd_compact_output(args):
    """fm compact-output [on|off] (T-0783): a PostToolUse Bash hook in ~/.claude/settings.json that hands big outputs
    to the model as head, failure lines and tail with the full text in a file. Off is no hook at all (no cost)."""
    P = paths()
    s = _load(P["settings"], {})
    mode = next((m for g in ((s.get("hooks") or {}).get("PostToolUse") or []) for h in (g.get("hooks") or [])
                 for m in [_compact_mode(h)] if m), None) or "off"
    if args.state in (None, "status"):
        print(f"compact output: {mode}" + {
            "on": " (new sessions shorten Bash outputs over 6,000 characters)",
            "shadow": " (outputs pass untouched; " + _shadow_total() + ")",
            "off": " (fm compact-output on to shorten big outputs, shadow to measure first)"}[mode])
        return 0
    if args.state == mode:
        print(f"compact output: already {args.state}")
        return 0
    _backup(P)
    _without_compact(s)  # one mode at a time
    if args.state != "off":
        cmd = _compact_cmd() if args.state == "on" else shlex.quote(_compact_cmd()) + " --shadow"
        s.setdefault("hooks", {}).setdefault("PostToolUse", []).append(
            {"matcher": "Bash", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
    c.write_atomic(P["settings"], json.dumps(s, indent=2) + "\n")
    print(f"compact output: {args.state} in {P['settings']} (backup in {P['backups']}); new sessions pick it up")
    return 0


def cmd_uninstall(args):
    from sampo import serve
    if args.dry_run:
        units = list(serve.units())
        if units:
            print("Would stop and remove fm serve units (their Remote Control sessions end): " + ", ".join(units))
    else:
        for line in serve.stop_all():  # fm serve units live outside the manifest: found by their marker
            print(line)
    P = paths()
    m = _load(P["manifest"], None)
    if m is None:
        print("No Sampo install manifest; nothing to undo.")
        return
    # added by `fm plugins install` / `add-marketplace` after the user's yes; plugins go before their marketplaces
    removals = [("plugin", "plugins_installed", pid, ["uninstall", pid, "--scope", "user"])
                for pid in m.get("plugins_installed") or []] + \
               [("marketplace", "marketplaces_added", mk, ["marketplace", "remove", mk])
                for mk in m.get("marketplaces_added") or []]
    kept = {}  # what couldn't be removed stays in the manifest, so a later uninstall-user retries it
    for what, key, name, cmd in removals:
        if args.dry_run:
            print(f"Would remove {what} {name}")
            continue
        try:
            r = subprocess.run(["claude", "plugin", *cmd], capture_output=True, text=True, timeout=300)
            err = "" if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200] or f"exit {r.returncode}"
        except (OSError, subprocess.TimeoutExpired) as e:
            err = str(e)
        if err:
            kept.setdefault(key, []).append(name)
        print(f"Could not remove {what} {name}: {err}" if err else f"Removed {what} {name}")
    s = _load(P["settings"], {})
    new_s, actions = copy.deepcopy(s), []
    if (new_s.get("statusLine") or {}).get("command") == P["wrapper"]:
        if m.get("statusLine_original"):
            new_s["statusLine"] = m["statusLine_original"]
        else:
            new_s.pop("statusLine")
        actions.append("statusLine restored")
    perms = new_s.get("permissions")
    if isinstance(perms, dict):
        if "deny" in perms:
            ours = set(m.get("deny_added", []))
            perms["deny"] = [r for r in perms["deny"] if r not in ours]
            if not perms["deny"] and m.get("deny_created"):
                perms.pop("deny")
            actions.append("deny rules removed")
        if "defaultMode_original" in m:
            if m["defaultMode_original"] is None:
                perms.pop("defaultMode", None)
            else:
                perms["defaultMode"] = m["defaultMode_original"]
            was = m["defaultMode_original"]
            actions.append(f"permissions.defaultMode = {was}" if was is not None
                           else "permissions.defaultMode unset (as before)")  # T-0851: not "= None"
        if not perms and m.get("permissions_created"):
            new_s.pop("permissions")
    env = new_s.get("env")
    if isinstance(env, dict):
        for k, v in (m.get("env_added") or {}).items():
            if env.get(k) == v:
                env.pop(k)
        if not env and m.get("env_created"):
            new_s.pop("env")
    wt = new_s.get("worktree")
    if m.get("worktree_baseRef_set") and isinstance(wt, dict) and wt.get("baseRef") == "head":
        wt.pop("baseRef")
        if not wt and m.get("worktree_created"):
            new_s.pop("worktree")
        actions.append("worktree.baseRef removed")
    for key, tag in (("model", "model"), ("switchModelsOnFlag", "flag")):  # T-3742: only what is still ours
        was = m.get(tag + "_original")
        if was and new_s.get(key) == m.get(tag + "_set"):
            if was.get("had"):
                new_s[key] = was.get("value")
            else:
                new_s.pop(key, None)
            actions.append(f"{key} restored")
    if m.get("outputStyle_set") and new_s.get("outputStyle") in OUTPUT_STYLES:
        new_s.pop("outputStyle")
        actions.append("outputStyle removed")
    if _without_compact(new_s):  # T-0783: fm compact-output on's hook goes with the rest
        actions.append("compact-output hook removed")
    for k in ("enabledPlugins", "extraKnownMarketplaces"):  # emptied by `claude plugin uninstall`/`marketplace remove`
        if new_s.get(k) == {}:
            new_s.pop(k)
    md = _read(P["claude_md"])
    new_md = md
    if md is not None and BEGIN in md:
        new_md = re.sub(re.escape(m.get("claude_md_sep", "")) + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", md,
                        count=1, flags=re.S)
        actions.append("CLAUDE.md block removed")
    if args.dry_run:
        print("Would change:\n  - " + "\n  - ".join(actions + ["rules symlink removed", "manifest removed"]))
        return
    _backup(P)
    if not new_s:  # T-0851: nothing left; `claude plugin install` may have made it as {} before the wiring ran
        if os.path.exists(P["settings"]):
            os.remove(P["settings"])
    elif new_s != s:
        c.write_atomic(P["settings"], json.dumps(new_s, indent=2) + "\n")
    if new_md != md:
        if not new_md and m.get("claude_md_created"):
            os.remove(P["claude_md"])
        else:
            c.write_atomic(P["claude_md"], new_md)
    if _link_state(P) == "ours":
        os.unlink(P["rules_link"])
        actions.append("rules symlink removed")
        try:
            os.rmdir(os.path.dirname(P["rules_link"]))  # T-0851: only when empty
        except OSError:
            pass
    if os.path.islink(P["fm_link"]) and os.path.realpath(P["fm_link"]) == os.path.realpath(P["fm_target"]):
        os.unlink(P["fm_link"])  # T-0853: ours only
        actions.append(f"{P['fm_link']} removed")
    if kept:
        c.write_atomic(P["manifest"], json.dumps(kept, indent=2) + "\n")
        actions.append("manifest kept for what couldn't be removed: fm uninstall-user retries it, "
                       "fm plugins forget ID leaves one in place")
    else:
        os.remove(P["manifest"])
    print("Removed Sampo's wiring from ~/.claude:\n  - " + "\n  - ".join(actions))


def _usage(msg):
    from sampo import cli
    return cli.UsageError(msg)
