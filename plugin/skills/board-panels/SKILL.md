---
name: board-panels
description: "Use whenever a diagram, dashboard or architecture overview is drawn: panels of the repo's real parts."
---

# Sampo: board panels

A diagram that names no real part teaches nothing, and a dashboard of invented numbers lies. So a diagram, dashboard
or architecture overview (for a README, docs, an app page or a report) is planned from the repo, drawn by the panel
kit, and checked before it ships: nobody has to ask for the plan, the check or the screenshot.

1. **Plan from the repo.** `sampo panels plan ROOT --out docs/assets/panels/spec.json` reads the services, routes,
   queues, jobs, stores, pipelines, tests and deploy files and writes a spec: an `arch` box-and-packet panel of how
   the parts connect, a `funnel` of the jobs into a queue, `lanes` of a CI pipeline's stages, a `filetree` of the
   store and entry files, a `terminal` of commands and tests, `tanks` for the compose services or build stages. Every
   part cites its file (`--json` prints them with what was left out and why). An empty repo gives no panels and says
   why: don't draw one anyway.
2. **Edit the spec, never invent.** Keep the panels the reader needs and shorten titles and `foot` lines in plain
   words; a part you add must be one you opened (`sampo sym PATH:NAME`). A number is a count the scan made or one you
   measured, with its source in the `foot`. `sampo panels kinds` lists what each kind draws.
3. **Render.** `sampo panels render docs/assets/panels/spec.json` draws a dark and a light SVG per panel in the repo's
   own colours (`--style board|blueprint|paper`) and prints the `<picture>` snippets to paste. A spec that breaks the
   kit's limits, or a panel whose text shows a private string, is refused and named: fix the spec, not the check.
4. **Check it.** `sampo readme check` holds each image to its file, alt text, dark/light pair and size budget; then
   open every SVG and read it as a stranger would: every label a real name, nothing clipped, nothing crowded. For the
   look as a whole: `../playbooks/references/ui/visual-qa.md`.
5. **Screenshot the page that shows them** (`sampo readme shots --cmd "<start command>" --routes /`, step 2 of
   `../readme-visuals/SKILL.md`) in dark and light, and look at each shot before it ships.
6. **Say it in the close:** `sampo task finish ID --docs "panels: <the diagrams added and where>"`.

A README or docs page around the panels follows `../write-docs/SKILL.md` and `../readme-visuals/SKILL.md`. A chart of
measured numbers follows `../playbooks/references/ui/data-viz.md`.
