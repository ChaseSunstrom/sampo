#!/usr/bin/env bash
# Reset Claude Code's *behavior layer* to a clean baseline. Dry run unless --apply.
#
# Usage: reset-claude.sh [--apply] [--all-plugins] [--keep <id,id,...>] [--force]
#   --apply         actually do it (the default only prints the plan)
#   --all-plugins   remove every plugin, including the ones Foreman keeps
#   --keep ids      also keep these plugin@marketplace ids (comma-separated)
#   --force         run even if Claude Code sessions are open (not recommended)
#
# Always: a full backup of ~/.claude and ~/.claude.json first, into ~/.claude-reset/<timestamp>/.
# Reset:  plugins (except the keep list), user-level hooks, permissions, non-HUD statusline,
#         ECC_* env vars, and ~/.claude/{CLAUDE.md,rules,skills,commands,agents,output-styles,hooks}
#         (moved into the archive, not deleted).
# Kept:   login, MCP servers and folder trust (~/.claude.json), session history and auto memory
#         (~/.claude/projects), themes, keybindings, other preferences, per-repo .claude/ dirs.
set -uo pipefail

APPLY=0 ALL=0 FORCE=0 EXTRA_KEEP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --all-plugins) ALL=1 ;;
    --force) FORCE=1 ;;
    --keep) EXTRA_KEEP="${2:-}"; shift ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "reset-claude: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done

CH="$HOME/.claude"
say()  { printf '%s\n' "$*"; }
act()  { if [ "$APPLY" = 1 ]; then "$@"; else echo "    would run: $*"; fi; }
for bin in claude python3 tar; do command -v "$bin" >/dev/null 2>&1 || { echo "reset-claude: '$bin' is required" >&2; exit 1; }; done
[ -d "$CH" ] || { say "Nothing to reset: $CH doesn't exist."; exit 0; }

if [ "$FORCE" = 0 ] && pgrep -x claude >/dev/null 2>&1; then
  echo "reset-claude: Claude Code is running. Close every session first (or pass --force)." >&2
  exit 1
fi

# Plugins Foreman keeps by default (BUILD_PROMPT.md §4.6); everything else is removed.
OFF=claude-plugins-official
OURS="sampo@sampo sampo-ui@sampo sampo@foreman sampo-ui@foreman foreman@foreman foreman-ui@foreman"  # T-2879/T-3707: the ids now and before
KEEP="$OURS ponytail@ponytail claude-hud@claude-hud context7@$OFF security-guidance@$OFF code-review@$OFF skill-creator@$OFF claude-md-management@$OFF plugin-dev@$OFF frontend-design@$OFF clangd-lsp@$OFF pyright-lsp@$OFF typescript-lsp@$OFF gopls-lsp@$OFF rust-analyzer-lsp@$OFF document-skills@anthropic-agent-skills differential-review@trailofbits insecure-defaults@trailofbits static-analysis@trailofbits sharp-edges@trailofbits supply-chain-risk-auditor@trailofbits c-review@trailofbits"
[ "$ALL" = 1 ] && KEEP="$OURS"
KEEP="$KEEP ${EXTRA_KEEP//,/ }"
kept() { case " $KEEP " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$HOME/.claude-reset/$TS"
[ "$APPLY" = 1 ] && say "Applying reset (archive: $ARCHIVE)" || say "Dry run. Nothing will change; re-run with --apply."

# 1. Full backup first
say "1. Back up ~/.claude and ~/.claude.json"
if [ "$APPLY" = 1 ]; then
  mkdir -p "$ARCHIVE/moved"
  items=(.claude); [ -f "$HOME/.claude.json" ] && items+=(.claude.json)
  tar -czf "$ARCHIVE/claude-home.tgz" -C "$HOME" "${items[@]}" 2>/dev/null \
    || { echo "reset-claude: backup failed; nothing was changed" >&2; exit 1; }
  say "   saved $ARCHIVE/claude-home.tgz ($(du -h "$ARCHIVE/claude-home.tgz" | cut -f1))"
else
  say "   would save $ARCHIVE/claude-home.tgz"
fi

# 2. Plugins
say "2. Plugins"
PLUGINS="$(claude plugin list --json 2>/dev/null | python3 -c 'import json,sys
for p in json.load(sys.stdin): print(p["id"])' 2>/dev/null)"
REMOVED_ANY=0
while read -r id; do
  [ -z "$id" ] && continue
  case "$id" in
    *@synced) say "   · $id is synced from claude.ai (turn it off there or with: claude plugin disable $id)" ;;
    *) if kept "$id"; then say "   keep   $id"
       else say "   remove $id"; act claude plugin uninstall "$id" --scope user >/dev/null 2>&1; REMOVED_ANY=1; fi ;;
  esac
done <<<"$PLUGINS"

# Marketplaces left with no installed plugins (official + foreman always stay)
if [ "$APPLY" = 1 ] && [ "$REMOVED_ANY" = 1 ]; then
  STILL="$(claude plugin list --json 2>/dev/null | python3 -c 'import json,sys
print(" ".join(p["id"].split("@",1)[1] for p in json.load(sys.stdin)))' 2>/dev/null)"
else
  STILL="$(for id in $PLUGINS; do kept "$id" && echo "${id#*@}"; done | tr '\n' ' ')"
fi
while read -r mp; do
  [ -z "$mp" ] && continue
  case " $OFF foreman $STILL " in *" $mp "*) continue ;; esac
  say "   remove marketplace $mp"; act claude plugin marketplace remove "$mp" >/dev/null 2>&1
done <<<"$(claude plugin marketplace list --json 2>/dev/null | python3 -c 'import json,sys
for m in json.load(sys.stdin): print(m["name"])' 2>/dev/null)"

# 3. settings.json: strip behavior keys, keep preferences
say "3. ~/.claude/settings.json"
if [ -f "$CH/settings.json" ]; then
  python3 - "$CH/settings.json" "$APPLY" <<'PY'
import json, os, sys
path, apply = sys.argv[1], sys.argv[2] == "1"
try:
    d = json.load(open(path))
except Exception as e:
    print(f"   ! couldn't parse settings.json ({e}); leaving it alone"); sys.exit(0)
changes = []
for key in ("hooks", "permissions"):
    if key in d: changes.append(f"remove {key}"); d.pop(key)
sl = d.get("statusLine")
if sl and "claude-hud" not in json.dumps(sl):
    changes.append("remove statusLine (not claude-hud)"); d.pop("statusLine")
env = d.get("env") or {}
for k in [k for k in env if k.startswith("ECC_")]:
    changes.append(f"remove env {k}"); env.pop(k)
if "env" in d and not env: d.pop("env")
for c in changes: print(f"   {c}")
if env: print(f"   · review remaining env keys yourself: {', '.join(sorted(env))}")
if not changes: print("   nothing to change")
if apply and changes:
    tmp = path + ".reset-tmp"
    with open(tmp, "w") as f: json.dump(d, f, indent=2); f.write("\n")
    os.replace(tmp, path)
PY
else
  say "   none"
fi

# 4. User-level instructions and extensions: move into the archive
say "4. User-level instructions and extensions (moved to the archive, not deleted)"
MOVED=0
for name in CLAUDE.md rules skills commands agents output-styles hooks; do
  [ -e "$CH/$name" ] || continue
  MOVED=1; say "   move ~/.claude/$name"
  act mv "$CH/$name" "$ARCHIVE/moved/$name"
done
[ "$MOVED" = 0 ] && say "   none"

# 5. Report what stays
say "5. Kept as is"
say "   login, MCP servers, folder trust (~/.claude.json); history + auto memory (~/.claude/projects); themes, keybindings"
KNOWN=" CLAUDE.md rules skills commands agents output-styles hooks settings.json plugins projects themes keybindings.json .credentials.json backups todos shell-snapshots statsig ide sessions foreman "
OTHER=""
for p in "$CH"/* "$CH"/.[!.]*; do
  [ -e "$p" ] || continue
  b="$(basename "$p")"
  case "$KNOWN" in *" $b "*) ;; *) OTHER="$OTHER $b" ;; esac
done
[ -n "$OTHER" ] && say "   · other entries left alone (review; some may be plugin leftovers):$OTHER"

if [ "$APPLY" = 1 ]; then
  say ""
  say "Done. Undo everything with:  tar -xzf $ARCHIVE/claude-home.tgz -C ~"
  say "Next: ~/.claude/foreman/install.sh --build   (or ./install.sh --build from the repo)"
fi
