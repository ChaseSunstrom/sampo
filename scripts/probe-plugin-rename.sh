#!/usr/bin/env bash
# T-2879: how a plugin rename moves an existing install, in a throwaway Claude config only (CLAUDE_CONFIG_DIR under
# TMPDIR; the user's own config is never read or written). It takes this repo's own manifests: installs foreman@foreman
# and foreman-ui@foreman from them as they were (every name mapped back through the renames map), sets a mod option,
# swaps in the manifests as they are, starts one session and prints a ✓/✗ line per check: the install moved, nothing
# loads twice, the hooks ran once and know the name they were loaded under, the mod's options moved, sampo:* commands,
# skills and agents exist. Exit 1 when a check fails.
#   INSTALL_FIRST=1: `claude plugin install sampo@foreman` runs on the swapped manifests before the session (what
#   install.sh did before the review; it now leaves an install that still has the old id to move by itself).
# Step 1's toy probes (a renames map, shims that depend on sampo, a renamed marketplace) are in research T-2879-probe.
set -euo pipefail
W=$(mktemp -d "${TMPDIR:-/tmp}/sampo-rename-probe.XXXXXX")
trap 'rm -rf "$W"' EXIT
export CLAUDE_CONFIG_DIR="$W/config"
mkdir -p "$CLAUDE_CONFIG_DIR"
REPO=$(cd "$(dirname "$0")/.." && pwd)
M="$W/mkt"; LOG="$W/hooks.log"; fails=0
check() { if eval "$2"; then echo "✓ $1"; else echo "✗ $1"; fails=$((fails + 1)); fi; }
# build old|new: each plugin the repo's marketplace lists, at its own source path, with its real plugin.json (name
# mapped back for old) and toy parts: a command, a skill, an agent and a SessionStart hook that logs its name and data dir
build() {
  python3 - "$REPO" "$M" "$1" "$LOG" <<'PY'
import json, os, sys
repo, m, mode, log = sys.argv[1:]
mk = json.load(open(os.path.join(repo, ".claude-plugin", "marketplace.json")))
back = {new: old for old, new in (mk.get("renames") or {}).items()}
for e in mk["plugins"]:
    name = back.get(e["name"], e["name"]) if mode == "old" else e["name"]
    pj = json.load(open(os.path.join(repo, e["source"], ".claude-plugin", "plugin.json")))
    pj = {k: v for k, v in pj.items() if k != "types"}  # the mod's types file is not copied
    pj["name"] = e["name"] = name
    d = os.path.join(m, e["source"])
    for sub in (".claude-plugin", "commands", "skills/hello", "agents", "hooks"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    w = lambda p, s: open(os.path.join(d, p), "w").write(s)
    w(".claude-plugin/plugin.json", json.dumps(pj))
    w("commands/hello.md", "---\ndescription: say hello\n---\nSay hello.\n")
    w("skills/hello/SKILL.md", "---\nname: hello\ndescription: hello skill\n---\nHello.\n")
    w("agents/fm-hello.md", "---\nname: fm-hello\ndescription: hello agent\n---\nSay hello.\n")
    hook = {"type": "command", "command": f"echo {name} >> {log}; echo {name} \"$CLAUDE_PLUGIN_DATA\" >> {log}.data"}
    w("hooks/hooks.json", json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}))
if mode == "old":
    mk.pop("renames", None)
    mk["name"] = "foreman"  # T-3707: the marketplace is sampo now; the old install was added as foreman (and keeps it)
os.makedirs(os.path.join(m, ".claude-plugin"), exist_ok=True)
json.dump(mk, open(os.path.join(m, ".claude-plugin", "marketplace.json"), "w"))
PY
}
settings() { python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(json.dumps({k: s.get(k) for k in ("enabledPlugins", "pluginConfigs")}))' "$CLAUDE_CONFIG_DIR/settings.json"; }
session() { : > "$LOG"; : > "$LOG.data"; timeout 90 claude -p "hi" </dev/null >/dev/null 2>&1 || true; }
ran() { sort "$LOG" | tr '\n' ' '; }
data() { sort "$LOG.data" | sed 's#  *.*/plugins/data/# #' | tr '\n' ' '; }  # name, then its data folder's name
build old
claude plugin marketplace add "$M" >/dev/null 2>&1 || { echo "marketplace add failed"; exit 1; }
claude plugin install foreman@foreman >/dev/null 2>&1 || { echo "install foreman@foreman failed"; exit 1; }
claude plugin install foreman-ui@foreman >/dev/null 2>&1 || { echo "install foreman-ui@foreman failed"; exit 1; }
python3 -c 'import json,sys; p=sys.argv[1]; s=json.load(open(p)); s["pluginConfigs"]={"foreman-ui@foreman": {"options": {"mascot": "blue"}}}; json.dump(s, open(p, "w"))' "$CLAUDE_CONFIG_DIR/settings.json"
session
echo "-- before (today's manifests): $(settings)"
echo "   hooks that ran, and their data folders: $(data)"
check "the old install loads: foreman's and foreman-ui's hooks ran once each" '[ "$(ran)" = "foreman foreman-ui " ]'
check "its hooks see the old name in CLAUDE_PLUGIN_DATA (foreman-foreman: c.loaded_ns says foreman)" '[ "$(data)" = "foreman foreman-foreman foreman-ui foreman-ui-foreman " ]'
rm -rf "$M"; build new
if [ "${INSTALL_FIRST:-}" = 1 ]; then  # an install of the new ids before any session moved the old ones
  claude plugin install sampo@foreman >/dev/null 2>&1 || echo "   install sampo@foreman failed"
  claude plugin install sampo-ui@foreman >/dev/null 2>&1 || echo "   install sampo-ui@foreman failed"
  echo "-- the new ids installed on the unmoved install: $(settings)"
fi
session
echo "-- after one session start on the new manifests: $(settings)"
echo "   hooks that ran, and their data folders: $(data)"
claude plugin list 2>&1 | sed 's/^/   plugin: /' | head -12
check "sampo@foreman and sampo-ui@foreman are enabled" 'settings | grep -q "\"sampo@foreman\": true" && settings | grep -q "\"sampo-ui@foreman\": true"'
check "no foreman@foreman or foreman-ui@foreman left enabled (no double load)" '! settings | grep -q "\"foreman\(-ui\)\?@foreman\""'
check "the mod's options moved with it (sampo-ui@foreman: mascot blue)" 'settings | grep -q "\"sampo-ui@foreman\": {\"options\": {\"mascot\": \"blue\"}}"'
check "the hooks ran once each, under the new names" '[ "$(ran)" = "sampo sampo-ui " ]'
check "and see the new name in CLAUDE_PLUGIN_DATA (sampo-foreman)" '[ "$(data)" = "sampo sampo-foreman sampo-ui sampo-ui-foreman " ]'
details=$(claude plugin details sampo@foreman 2>&1 || true)
echo "$details" | grep -iE 'hello|command|skill|agent|hook' | sed 's/^/   details: /' | head -8
check "sampo@foreman carries the hello command and skill and the fm-hello agent (/sampo:hello, sampo:fm-hello)" 'echo "$details" | grep -q "hello" && echo "$details" | grep -q "fm-hello"'
claude plugin install sampo@foreman >/dev/null 2>&1 || true  # install.sh run again on a moved install
session
check "install.sh's install of sampo@foreman on a moved install loads it once" '[ "$(ran)" = "sampo sampo-ui " ]'
[ "$fails" = 0 ] && echo "all checks passed" || { echo "$fails check(s) failed"; exit 1; }
