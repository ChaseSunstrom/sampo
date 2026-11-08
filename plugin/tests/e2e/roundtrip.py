#!/usr/bin/env python3
"""Scenario 12: install → uninstall → reinstall on a sandbox HOME and a scratch clone of the repo.

Proves uninstall leaves nothing behind except (optionally) state/, restores the original statusLine and
permission mode, and that a reinstall wires identically. The real ~/.claude is never touched.
Usage: roundtrip.py [--original <settings.json to start from>]   (exit 1 on any failure)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def run(cmd, env, cwd=None):
    p = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def main():
    argv = sys.argv
    original = argv[argv.index("--original") + 1] if "--original" in argv else None
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}", flush=True)

    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache")
    with tempfile.TemporaryDirectory(dir=base, prefix="fm-roundtrip-") as tmp:
        home = os.path.join(tmp, "home")
        claude_dir = os.path.join(home, ".claude")
        fhome = os.path.join(claude_dir, "foreman")
        os.makedirs(claude_dir)
        settings = (load(original) if original else None) or \
            {"permissions": {"defaultMode": "auto"}, "statusLine": {"type": "command", "command": "echo orig"}}
        settings.get("enabledPlugins", {}).pop("foreman@foreman", None)
        settings.get("extraKnownMarketplaces", {}).pop("foreman", None)
        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            json.dump(settings, f, indent=2)
        with open(os.path.join(claude_dir, "CLAUDE.md"), "w") as f:
            f.write("# Me\n- prefer short answers\n")
        orig_settings = load(os.path.join(claude_dir, "settings.json"))
        orig_md = read(os.path.join(claude_dir, "CLAUDE.md"))
        branch = subprocess.run(["git", "-C", REPO, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                text=True).stdout.strip()
        # detached HEAD (a lane train's scratch tree): no --branch, the clone checks out the source's commit
        rc, out = run(["git", "clone", "-q"] + ([] if branch == "HEAD" else ["--branch", branch]) + [REPO, fhome],
                      os.environ)
        want = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        got = subprocess.run(["git", "-C", fhome, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        if rc == 0 and got != want:  # clone landed elsewhere: pin to the source commit
            rc, out = run(["git", "-C", fhome, "checkout", "-q", want], os.environ)
        check("clone repo into sandbox FOREMAN_HOME", rc == 0, out.strip()[-200:])
        env = dict(os.environ, HOME=home, FOREMAN_HOME=fhome)
        env.pop("CLAUDE_CONFIG_DIR", None)

        def plugin_ids():
            _, out = run(["claude", "plugin", "list", "--json"], env)
            try:
                return {p["id"] for p in json.loads(out[out.index("["):])}
            except (ValueError, KeyError):
                return set()

        # ---- install
        rc, out = run([os.path.join(fhome, "install.sh"), "--no-plugins", "--bypass"], env)  # T-0871: opted in
        check("install.sh --no-plugins exits 0", rc == 0, out.strip().splitlines()[-1] if out.strip() else "")
        s1 = load(os.path.join(claude_dir, "settings.json")) or {}
        wrapper = os.path.join(fhome, "plugin", "hooks", "statusline")
        check("installed: statusLine is the Foreman wrapper", (s1.get("statusLine") or {}).get("command") == wrapper)
        deny1 = (s1.get("permissions") or {}).get("deny", [])  # T-3704: no rm rule (CC reads "*" as a wildcard and asks)
        check("installed: deny rules + drive env", len(deny1) >= 10 and "Bash(mkfs *)" in deny1 and
              not [r for r in deny1 if r.startswith("Bash(rm ")] and "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP" in (s1.get("env") or {}))
        check("installed: bypass default mode", (s1.get("permissions") or {}).get("defaultMode") == "bypassPermissions")
        check("installed: CLAUDE.md block", "<!-- foreman:begin -->" in (read(os.path.join(claude_dir, "CLAUDE.md")) or ""))
        link = os.path.join(claude_dir, "rules", "foreman.md")
        check("installed: rules symlink", os.path.islink(link) and os.path.realpath(link).startswith(fhome))
        check("installed: plugin foreman@foreman", "foreman@foreman" in plugin_ids())

        # ---- uninstall
        rc, out = run([os.path.join(fhome, "plugin", "uninstall.sh"), "--yes", "--purge-state"], env)
        check("uninstall.sh exits 0", rc == 0, out.strip().splitlines()[-1] if out.strip() else "")
        s2 = load(os.path.join(claude_dir, "settings.json")) or {}
        diff = {k: s2.get(k) for k in set(s2) | set(orig_settings) if s2.get(k) != orig_settings.get(k)}
        check("uninstalled: settings.json identical to the original", not diff, json.dumps(diff)[:300])
        check("uninstalled: original statusLine and permission mode restored",
              s2.get("statusLine") == orig_settings.get("statusLine") and
              (s2.get("permissions") or {}).get("defaultMode") == (orig_settings.get("permissions") or {}).get("defaultMode"))
        check("uninstalled: CLAUDE.md identical", read(os.path.join(claude_dir, "CLAUDE.md")) == orig_md)
        check("uninstalled: no rules symlink", not os.path.lexists(link))
        check("uninstalled: plugin removed", "foreman@foreman" not in plugin_ids())
        _, out = run(["claude", "plugin", "marketplace", "list"], env)
        check("uninstalled: marketplace removed", "foreman" not in out.lower().replace("foreman@", ""), out.strip()[-120:])
        backups = os.path.join(fhome, "backups")
        archives = [f for f in os.listdir(backups) if f.startswith("state-")] if os.path.isdir(backups) else []
        check("uninstalled: state/ removed only after archiving", not os.path.exists(os.path.join(fhome, "state")) and archives)

        # ---- reinstall
        rc, out = run([os.path.join(fhome, "install.sh"), "--no-plugins", "--bypass"], env)
        s3 = load(os.path.join(claude_dir, "settings.json")) or {}
        diff = {k: s3.get(k) for k in set(s3) | set(s1) if s3.get(k) != s1.get(k)}
        check("reinstall: identical wiring", rc == 0 and not diff, json.dumps(diff)[:300])
        run([os.path.join(fhome, "plugin", "uninstall.sh"), "--yes"], env)
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [c for c in checks if not c[1]]
    print(f"\nroundtrip: {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
