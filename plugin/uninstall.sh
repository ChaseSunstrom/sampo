#!/usr/bin/env bash
# Remove Foreman from this machine.
#   1. uninstall the sampo plugin and its mod (under their foreman- names on an install from before T-2879)
#      and the local "foreman" marketplace
#   2. undo the ~/.claude wiring recorded in state/install-manifest.json (statusLine, deny rules, env,
#      CLAUDE.md block, rules symlink, permission mode, plugin maps left empty by step 1)
#   3. keep state/ unless you confirm (it is archived to backups/ before it is removed)
# Usage: plugin/uninstall.sh [--dry-run] [--purge-state] [--yes]
# Plugins Foreman disabled are listed with the command to re-enable them; nothing else is touched.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FOREMAN_HOME="${FOREMAN_HOME:-$(dirname "$HERE")}"
export FOREMAN_HOME
DRY=0 PURGE=0 YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --purge-state) PURGE=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "uninstall: unknown option '$arg'" >&2; exit 2 ;;
  esac
done
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then echo "    (dry-run) $*"; else "$@"; fi; }

# The state dir in use (FOREMAN_STATE, <home>/state, or the read-only-home fallback), as fm resolves it.
state="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from sampo import core; print(core.state_dir())' "$HERE/lib" 2>/dev/null \
  || echo "$FOREMAN_HOME/state")"
manifest="$state/install-manifest.json"
if [ -f "$manifest" ]; then
  disabled="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get("plugins_disabled", [])))' "$manifest" 2>/dev/null || true)"
else
  disabled=""
fi

say "Uninstalling the plugin and the local marketplace"
for m in sampo foreman; do  # T-2879/T-3707: the marketplace and plugin names now and before; an install has one id
  for n in sampo foreman; do
    run claude plugin uninstall "$n-ui@$m" --scope user >/dev/null 2>&1 || true
    run claude plugin uninstall "$n@$m" --scope user >/dev/null 2>&1 && gone=1
  done
  run claude plugin marketplace remove "$m" >/dev/null 2>&1 && market=1
done
[ -n "${gone:-}" ] || say "  (the sampo plugin was not installed)"
[ -n "${market:-}" ] || say "  (marketplace 'sampo' was not registered)"

say "Undoing the ~/.claude wiring"  # after the plugin removal, so the maps it empties are cleaned up too
if [ "$DRY" = 1 ]; then python3 "$HERE/bin/fm" uninstall-user --dry-run; else python3 "$HERE/bin/fm" uninstall-user; fi

if [ -d "$state" ]; then
  if [ "$PURGE" = 1 ] && { [ "$YES" = 1 ] || { [ -r /dev/tty ] && read -r -p "Delete $state (archived to backups/ first)? [y/N] " ans </dev/tty && [ "$ans" = y ]; }; }; then
    archive="$FOREMAN_HOME/backups/state-$(date +%Y%m%d-%H%M%S).tgz"
    run mkdir -p "$FOREMAN_HOME/backups"
    run tar -C "$(dirname "$state")" -czf "$archive" "$(basename "$state")"
    run rm -r "$state"
    say "State archived to $archive and removed ($state)"
  else
    say "Kept $state (tasks, ledger, decisions). Remove it with --purge-state."
  fi
fi

if [ -n "$disabled" ]; then
  say "Foreman disabled these plugins; re-enable any you want:"
  for p in $disabled; do echo "    claude plugin enable $p"; done
fi
say "Done. The repo itself stays at $FOREMAN_HOME (delete it if you no longer want it)."
