---
name: capture
description: Capture a request into the Sampo inbox without working on it (usage /sampo:capture <text>): an idea queued for later.
argument-hint: "<request text>"
disable-model-invocation: true
---

# Sampo: capture

Capture `$ARGUMENTS` verbatim: guess its type (FIX/FEATURE/CLEAN/PERFORMANCE/SECURITY/RESEARCH) and tier (S/M/L), then
`sampo capture "$ARGUMENTS" --type <T> --tier <X>`.
Reply in one line: "Captured as T-0019 [FEATURE, M], queued after the current task. Say NOW to switch." Do not start it.
