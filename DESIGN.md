# DESIGN.md: Sampo

Tokens live in `design/sampo/tokens.json` (the source of truth for colour, type and motion); do not copy values here.

## Point of view

Sampo's live page and site read like an instrument panel in a well-made workshop: a dense dashboard a developer glances at all day, in the manner of Grafana or Linear, with real charts drawn from real task, lane and cost data. Surfaces have material lighting (a lit edge, a soft shadow, depth between panel and page), never flat fills or cartoon outlines. Things arrive with a short, cinematic reveal and then stay alive with slow, purposeful motion. The audience is one developer running many agents; the first thing they must see is what is running and what needs them.

## Direction

audacity: bold

- Default look is a dashboard: a filled dock, compact rows (about 28 px tall, one line each), type steps one notch below a marketing page. Nothing oversized.
- Charts are real: tanks for budgets and capacity, lane plots for parallel work, sparklines only as a hint beside a number, never as the only display of it. No tiny inline bars standing in for a chart.
- The illustrated forge (Ilmarinen's workshop) is a setting the user turns on, never the default. When on, it is detailed and fully animated, never cartoonish or "goofy".
- Declutter rows: one primary fact per row, secondary facts muted and right-aligned, actions shown on hover or focus.
- Motion: a 400 to 700 ms arrival per panel, staggered; live data eases over 200 ms; honour `prefers-reduced-motion` by dropping to opacity fades.
- Data is never invented: an empty state says so; no placeholder numbers.
- Both themes (light and dark) ship together, each passing WCAG AA contrast; every view works at phone width (360 px) with the dock collapsing to a bottom bar.
