# foreman-ui

Sampo inside Claude Code: the docked pane, the band above the prompt, toasts and one-press actions. It installs as
`sampo-ui` (this folder keeps its old name):

    /plugin install sampo-ui --marketplace ChaseSunstrom/sampo

Tests: `claude plugin test mods/foreman-ui`.

- `tools/pane_replay.py render|twin FIXTURE` (T-1534): the pane drawn from a recorded `fm ui --json` view with no live
  session, through this mod's test kit; `twin` diffs two runs (`--matrix`: a third in another time zone and locale).
  Synthetic fixtures only in `tests/fixtures/`.
- Grants (T-1262): Ward asks for each `fm ask`; this mod answers that ask as its own (`tool.check`), so
  Claude Code puts it only to a person (the dialog, a host's callback or a PermissionRequest hook): auto mode's
  classifier, bypass mode or a saved rule for all of Bash can't approve it, and where nobody can be asked it is refused.
- `tools/shot.py` (T-0131): Claude Code with this mod in a tmux harness of its own, the screen as a PNG.
