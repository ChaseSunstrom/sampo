---
name: readme-visuals
description: "Use whenever a README, docs or landing page is drafted or reworked: app screenshots, SVG diagrams, dark+light."
---

# Sampo: README visuals

A reader decides in seconds whether a project is for them, and words alone rarely carry it: they need to see the
product, how it works, and what it costs. So a README (or a docs or landing page) you write or rework gets figures
without anyone asking, and every figure is real: a screenshot of the running app, a diagram drawn from the code's own
parts, a chart of a measured number. The prose itself follows `../write-docs/SKILL.md`; this skill is its pictures.

1. **Plan the figures from the repo.** `sampo readme plan` reads the routes, API, commands, entry points and services
   and proposes each figure (kind, what it shows, where it goes); `--brief` writes the list into the active task.
   Keep the ones a reader needs: the product first (top, under the title), then how it works, then what it costs.
2. **Shoot the real app.** `sampo readme shots --cmd "<start command>" --routes /,/settings` (or `--url` for one
   already running; `--wait-for TEXT` when data loads late) starts it, waits until it answers, shoots each route at
   desktop and phone width, light and dark, writes PNGs under docs/assets/screens/ and stops it. Seed realistic demo
   data first: an empty screen sells nothing. A page whose text shows a private string (a home path, a private IP, an
   internal host, an email, a token, the login) is refused and named; fix the data, or `sampo private add` a term
   that is fine to show. The scan reads page text, not pixels: look at each shot before it ships.
3. **Draw how it works.** Write a spec and run `sampo readme diagram docs/assets/diagrams/architecture.json`: a dark
   and a light SVG, laid out by rank (never by hand), in the repo's DESIGN.md or CSS tokens or the Board look, gated
   by `sampo works check`'s source gates. Groups are the layers, nodes the real files, routes and services (a box
   labelled "Backend" teaches nothing), edges what calls what; `"motion": true` makes the edges flow:
   `{"title": "How it works", "groups": [{"id": "srv", "label": "Server", "sub": "FastAPI"}], "nodes": [{"id": "ui",
   "label": "index.html"}, {"id": "api", "label": "app.py", "sub": "/api", "group": "srv"}], "edges": [{"from": "ui",
   "to": "api", "label": "fetch"}]}`. A chart of measured numbers follows `../playbooks/references/ui/data-viz.md`.
4. **Ship each figure dark and light** in a `<picture>` (the verbs print the snippet): the dark file in a
   `<source media="(prefers-color-scheme: dark)">`, the light one in the `<img>`, which is also the fallback. Alt text
   says what the figure shows ("The watch page: a video grid with download progress"), never "screenshot".
5. **Check it all.** `sampo readme check` holds every image to its file, its alt text, its dark/light pair, a size
   budget (400 KB a PNG, 150 KB an SVG) and an SVG with no private text; exit 1 names each problem. For the page's
   look as a whole: `../playbooks/references/ui/visual-qa.md`.
6. **Say it in the close:** `sampo task finish ID --docs "README: <the figures added>"`.
