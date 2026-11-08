---
name: security-audit
description: Use whenever code takes user input, secrets, auth or an API route, or needs a security review: threat-check it.
---

# Sampo: security review

Security work is a SECURITY task: no brief yet → `/sampo:intake` first. Findings are proved before they are fixed, and a fix starts with a failing test that exploits the hole.

1. **Scan first.** `sampo secrets` finds credentials in the tree and config (git history with `--history`); a hit is rotated by its owner, not just deleted. `sampo deps` shows outdated or risky dependencies.
2. **Walk the checklist** in `../playbooks/references/security/security-review.md` for what the change touches: input validation, authentication and authorization, secrets, injection, output encoding, rate limits, error messages. Cloud or CI configuration → `../playbooks/references/security/cloud-infrastructure.md`.
3. **Trace each entry point:** `sampo graph blast PATH` for what reaches the code, `sampo sym PATH:NAME` for its callers. Mark which inputs an outsider controls.
4. **Hunting for holes** in a whole repo: `sampo find --rank` lists leads, `sampo prove` each one red before fixing it; `../playbooks/references/security/bounty-triage.md` (remotely reachable first). A guard or policy hook that blocks or allows wrongly → `../playbooks/references/security/command-guard.md`.
5. **Fix test-first.** The test sends the hostile input and fails; the fix turns it green: `sampo task evidence ID --step N --run "<that test>"`.
6. **Report each finding** with severity, the path it takes, and what you proved. One you can't prove is a question, not a finding. Close with `sampo audit prep ID` for one `sampo:fm-reviewer` pass.

Never put a real secret into a test, a log or a brief; use a synthetic value of the same shape. Proofs are local tests only: no live systems, no running untrusted code. Never loosen or edit the Ward to make a check pass; a rule change goes through `sampo ask ID core`.
