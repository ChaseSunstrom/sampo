---
name: ui-design
description: "Use whenever you build or restyle a page, component, animation or 3D scene: tokens, motion, QA."
---

# Sampo: UI design, motion and 3D

One path for any work on how a screen looks or moves. The full steps, with every command and playbook path, are in `references/procedure.md`. Read it before any step, and read it again after a compaction: this stub keeps only the step headings. Paths below are relative to this skill; a terminal UI or a Claude Code mod takes `../playbooks/references/ui/terminal-ui.md` instead.

Work inside a focused task (each step edits files). Per step, read only the playbook it names.

1. Direction: the repo's `DESIGN.md` wins when it has one. With no direction, load Skill sampo:directions. Real specimens: `sampo page looks`, `sampo looks`. Stance: `../playbooks/references/ui/design-direction.md`.
2. Tokens: `sampo design extract SRC` writes DESIGN.md's `## Tokens` section; `sampo design palette SEED --out tokens.css` makes a light and dark palette; `sampo design contrast TOKENS` proves the pairs. Playbook: `../playbooks/references/ui/type-and-colour.md`.
3. Build: `sampo new web DIR` (a page) or `sampo new scene DIR` (a 3D scene), then `sampo new check PATH`. Playbooks: `../playbooks/references/ui/composition.md`, `interaction.md`.
4. Motion: `../playbooks/references/ui/motion.md`; write `motion.toml`, then `sampo motion check` and `sampo motion compile`.
5. 3D: `../playbooks/references/ui/scene-3d.md`; describe the scene as data in `scene.json`, then `sampo scene compile`.
6. Check: `sampo design lint SRC`, `sampo design check`, `sampo critique URL`, `sampo smoke`, `sampo img diff A.png B.png`; Read the PNGs and look. Playbook: `../playbooks/references/ui/visual-qa.md`.
7. Taste: `sampo looks keep LOOK` or `sampo looks revert LOOK`, so the next ranking learns it.
