---
name: cc-monitors
title: Plugin monitors
area: claude-code
tags: [claude-code, monitors, background, plugin, notifications]
detect: [file:monitors/monitors.json]
related: [cc-plugins, cc-plugin-manifest, cc-hook-performance, cc-skills]
---
A monitor is a persistent background command a plugin starts in interactive sessions; each line it prints reaches Claude as a notification.

## Verify
- `python3 -m json.tool monitors/monitors.json`: the array parses.
- `claude plugin validate .`: monitors entries are strict, so an unknown key fails (path checks need 2.1.283 or later).
- `./bin/watch | head -3`: the command prints one line per event and flushes.
- `claude --plugin-dir ./plugin`: the monitor appears in the task panel once the session starts.

## Test first
Test the watcher script in isolation: it must emit exactly one line per new event and exit on EOF or signal.
```bash
printf 'a\nb\n' > /tmp/events
out=$(timeout 3 ./bin/watch --from-file /tmp/events | wc -l)
[ "$out" -eq 2 ] || { echo "expected 2 lines got $out"; exit 1; }
```
macOS has no `timeout`: use `gtimeout` (brew coreutils) there, or `perl -e 'alarm 3; exec @ARGV' ./bin/watch --from-file /tmp/events` beside it.

## Do
- Entry fields: `name` and `command` and `description` (all required), and optional `when`. Nothing else is accepted.
- Declare them in `monitors/monitors.json` (default path) or inline under `experimental.monitors` in the manifest.
- `when: "always"` (default) starts at session start and plugin reload; `"on-skill-invoke:<skill>"` starts the first time that skill runs, which saves resources.
- Wrap the plugin path in quotes in the command: `"\"${CLAUDE_PLUGIN_ROOT}\"/bin/watch"`.
- Print one short line per event and flush stdout; each line costs context when it wakes Claude.
- Make the script quiet while nothing happens and exit cleanly on a closed pipe or SIGTERM.
- Deduplicate and rate-limit inside the script.
- Fetch needed config inside the script; monitors get no user-config values from Claude Code.

## Avoid
- `${user_config.*}` in a monitor command: rejected. Read a settings file or environment inside the script.
- Chatty output (a line per second): each line is context and a wake-up. Batch or summarize.
- Expecting monitors in headless `-p` runs or on Amazon Bedrock, Google Cloud's Agent Platform or Microsoft Foundry: they run only in interactive sessions on supported providers.
- Starting a heavy watcher with `when: "always"` for a rarely used feature: use `on-skill-invoke`.
- Missing flush: output stays buffered and arrives late. Flush after every line.
- Treating the old top-level `monitors` key as current: it still loads but warns. Use `experimental.monitors` or the default file.

## Review
- [ ] Entries have `name`, `command`, `description` and nothing unknown.
- [ ] Output is low-volume, one line per event.
- [ ] Heavy watchers use `on-skill-invoke`.
- [ ] The plugin path is quoted in the command.
- [ ] The script exits cleanly on signals.
