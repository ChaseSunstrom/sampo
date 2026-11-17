"""The landing page's readings, drawn from data at build time (T-4842).

The Record's four gauges and its ledger, the Ward's blocked-command kinds, the dates and the counts beside them were
baked into site/index.html from a design-time run, so they went stale at the next landing. Each function here takes
site/data/stats.json's contents and returns markup; build.py puts it where index.html's {{gauges}}, {{ledger}} and
{{kinds}} stand. Stdlib only, like build.py.
"""
import datetime
import html
import math
import random

esc = html.escape
MONTHS = ("January February March April May June July August September October November December").split()
WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen twenty").split()
KINDS = {"core": "Edits to Sampo itself", "brief": "Edits with no task brief",
         "state-direct": "Writes to its state files directly", "system": "System files", "plugin": "Plugin files",
         "rm-outside": "Deletes outside the project", "pipe-shell": "Downloads piped into a shell",
         "credentials": "Credential reads"}


def num(n):
    """A count as the site writes it: 1,662 for hundreds and up, the figure itself below."""
    return f"{n:,.0f}" if isinstance(n, (int, float)) and abs(n) >= 100 else f"{n:g}"


def word(n):
    return WORDS[n] if 0 <= n < len(WORDS) else str(n)


def day_of(iso):
    try:
        return datetime.date.fromisoformat(str(iso)[:10])
    except ValueError:
        raise SystemExit(f"build.py: site/data/stats.json has a date that is not YYYY-MM-DD: {str(iso)[:20]!r}")


def long_day(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def long_date(iso):
    d = day_of(iso)
    return f"{long_day(d)} {d.year}"


def short_day(d):
    return f"{d.day} {MONTHS[d.month - 1][:3]}"


def span(first, last):
    """'2 October to 2 November 2026'; '7 to 9 December 2026' inside a month; the year twice across a year."""
    if first.year != last.year:
        return f"{long_day(first)} {first.year} to {long_day(last)} {last.year}"
    if first.month == last.month:
        return f"{first.day} to {long_day(last)} {last.year}"
    return f"{long_day(first)} to {long_day(last)} {last.year}"


def need(stats, *path):
    got = stats
    for key in path:
        if not isinstance(got, dict) or key not in got:
            raise SystemExit(f"build.py: site/data/stats.json has no {'.'.join(path)}; run site/stats.py"
                             + (" and site/bench_safety.py" if path[0] == "bench" else ""))
        got = got[key]
    return got


def kinds(stats):
    """The Ward's most common blocked-command kinds, widest first, and what the shown kinds leave out."""
    guard = need(stats, "guard")
    blocks = need(stats, "totals", "guard_blocks")
    rows = sorted(((str(k), int(v)) for k, v in guard.items()), key=lambda kv: -kv[1])
    top = max([v for _, v in rows] or [1]) or 1
    items = "".join(f'<li style="--v:{round(v / top, 3):g};--i:{i}"><span>{esc(KINDS.get(k) or k.replace("-", " ").capitalize())}'
                    f'</span><b>{v}</b></li>' for i, (k, v) in enumerate(rows))
    rest = blocks - sum(v for _, v in rows)
    tail = ""
    if rest > 0:
        tail = (f'<p class="fine x143">{word(rest).capitalize()} more block{" is" if rest == 1 else "s are"} of '
                f'{"a rarer kind" if rest == 1 else "rarer kinds"} or {"has" if rest == 1 else "have"} no kind recorded.</p>')
    return f'<ul class="kinds" id="kinds">{items}</ul>' + tail


def _nice(v, steps):
    return next((s for s in steps if v <= s), steps[-1] * math.ceil(v / steps[-1]))


def _gauge(i, big, text, v, mx, labels, hot, aria):
    w = 600 * min(v, mx) / mx
    rng = random.Random(i)
    s = [f'<svg viewBox="-8 0 616 76" role="img" aria-label="{esc(aria)}">',
         '<path d="M0 34 H600 L604 38 V52 L600 56 H0 L-4 52 V38 Z" fill="url(#brassPlate)"/>'
         '<path d="M0 34.6 H600" stroke="#FFF4D0" stroke-opacity=".8"/><path d="M0 55.4 H600" stroke="#3A2408" stroke-opacity=".6"/>']
    if hot:
        s.append(f'<path d="M{600 * hot / mx:.1f} 35 H600 V55 H{600 * hot / mx:.1f} Z" fill="#B12E16" opacity=".55"/>')
    ticks = "".join(f"M{t * 30} 35 V{46 if t % 4 == 0 else 41}" for t in range(21))
    s.append(f'<path d="{ticks}" stroke="#3A2408" stroke-width="1.2"/><path d="{ticks.replace(" 35 V", " 35.8 V")}" '
             'stroke="#FFF4D0" stroke-opacity=".5" stroke-width=".6" transform="translate(.9 0)"/>')
    top = [(0, 20)] + [(x, 20 + rng.uniform(-.7, .7)) for x in [w * q for q in (.2, .4, .6, .8)]] + [(w, 20), (w, 31)]
    bar = _poly(top + [(0, 31)])
    flakes = "".join(_poly([(x, y), (x + rng.uniform(1.4, 3), y - .6), (x + rng.uniform(2, 3.6), y + rng.uniform(.8, 1.6)),
                            (x + .4, y + 1.2)])
                     for x, y in ((rng.uniform(3, max(5, w * .5)), rng.uniform(22.5, 28.5)) for _ in range(int(w / 9) + 2)))
    s.append(f'<g class="fillg" style="--d:{i * .15:.2f}s"><path d="{bar}" fill="#000" opacity=".45" transform="translate(1 3)"/>'
             f'<path d="M{w - 26:.1f} 13 H{w + 12:.1f} V38 H{w - 26:.1f} Z" fill="url(#tipGlow)"/><path class="fill" d="{bar}"/>'
             f'<path d="M0 21 H{w:.1f}" stroke="#FFF1C8" stroke-opacity=".55"/><path d="{flakes}" fill="#2A0C06" opacity=".55"/>'
             f'<path d="M{max(0, w - 20):.1f} 20 H{w:.1f} V31 H{max(0, w - 20):.1f} Z" fill="url(#tipCore)"/>'
             f'<path d="M{w:.1f} 20 V31" stroke="#FFFBF0" stroke-width="1.2"/></g>')
    s += [f'<text x="{j * 120}" y="72" text-anchor="middle">{lab}</text>' for j, lab in enumerate(labels)]
    s.append("</svg>")
    return f'<figure class="gauge"><div class="read"><b>{esc(big)}</b><span>{esc(text)}</span></div>{"".join(s)}</figure>'


def _f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".") if abs(v - round(v)) > 1e-6 else str(int(round(v)))


def _poly(points):
    return "M" + " L".join(f"{_f(x)} {_f(y)}" for x, y in points) + " Z"


def gauges(stats):
    """Four forged scales: evidence runs per task, proofs per hundred tasks, the median full check, the Ward's
    median check on a shell command against its budget."""
    tasks, ev, proofs = (need(stats, "totals", k) for k in ("tasks_done", "evidence", "proofs"))
    runs, median = need(stats, "gate_seconds", "runs"), need(stats, "gate_seconds", "median")
    ward = need(stats, "bench", "PreToolUse:Bash")
    per_task = ev / tasks if tasks else 0.0
    per_hundred = 100 * proofs / tasks if tasks else 0.0
    mx1, mx2 = _nice(per_task, (10, 20, 50, 100)), _nice(per_hundred, (100,))
    mx3, mx4 = _nice(median, (600, 1200, 3600)), _nice(max(ward["p50"], ward["budget"]), (200, 400, 1000))  # the budget mark stays on the scale
    mins, secs = divmod(int(round(median)), 60)
    scale3 = f"{mx3 // 60} min"
    out = [
        _gauge(0, f"{per_task:.1f}", f"evidence runs per task: {num(ev)} runs over {num(tasks)} tasks", per_task, mx1,
               [f"{mx1 * j / 5:g}" for j in range(6)], None, f"{per_task:.1f}, on a scale of 0 to {mx1}"),
        _gauge(1, f"{per_hundred:.0f}", "proofs for every hundred tasks closed", per_hundred, mx2,
               [f"{mx2 * j / 5:g}" for j in range(5)] + [f"{mx2:g} %"], None, f"{per_hundred:.0f}, on a scale of 0 to {mx2} %"),
        _gauge(2, f"{mins} min {secs} s" if mins else f"{secs} s", f"the median full check, over {num(runs)} runs", median, mx3,
               [f"{mx3 * j / 5 / 60:g}" for j in range(5)] + [scale3], None,
               f"{mins} min {secs} s, on a scale of 0 to {scale3}" if mins else f"{secs} s, on a scale of 0 to {scale3}"),
        _gauge(3, f"{ward['p50']:g} ms", f"the Ward's median check on a shell command; red is over its {ward['budget']:g} ms budget",
               ward["p50"], mx4, [f"{mx4 * j / 5:g}" for j in range(5)] + [f"{mx4:g} ms"], ward["budget"],
               f"{ward['p50']:g} ms, on a scale of 0 to {mx4} ms"),
    ]
    return "".join(out)


def _days(daily):
    rows = []
    for r in daily:
        tasks = sum(v for k, v in r.items() if k not in ("day", "lanes", "evidence") and isinstance(v, int))
        rows.append((day_of(r["day"]), tasks, int(r.get("evidence") or 0)))
    return sorted(rows)


def _ledger_svg(days, peak, width, cls, step, vals):
    first, last = days[0][0], days[-1][0]
    by_day = {d: (t, e) for d, t, e in days}
    n = (last - first).days + 1
    bw, base, sc = width / n, 262, 230 / max(peak, 1)
    tid = f"ledger-t-{cls}"
    total = sum(t for _, t, _ in days)
    where = max(days, key=lambda r: r[1])[0]
    s = [f'<svg class="{cls}" viewBox="0 0 {width} 300" role="img" aria-labelledby="{tid}"><title id="{tid}">Tasks closed each day, '
         f'{span(first, last)}: {len(days)} working days, {num(total)} tasks in all, the most on {long_day(where)} with {peak}.</title>',
         f'<line class="base" x1="0" x2="{width}" y1="{base + .5}" y2="{base + .5}"/>']
    grid = _nice(peak / 5 if peak else 1, (10, 20, 50, 100, 200, 500, 1000, 2000, 5000))
    for v in range(grid, peak + 1, grid):
        y = base - v * sc
        s.append(f'<line class="tick" x1="0" x2="{width}" y1="{y:.1f}" y2="{y:.1f}" stroke-dasharray="2 6"/>'
                 f'<text x="0" y="{y - 5:.1f}">{v}</text>')
    for i in range(n):
        d = first + datetime.timedelta(days=i)
        x, w, cx = i * bw + bw * .22, bw * .56, i * bw + bw / 2
        t, e = by_day.get(d, (None, None))
        if t:
            y = base - max(3, t * sc)
            ings, caps, yy = [], [], base
            for j in range(0, t, 10):
                ih = min(10, t - j) * sc
                y1 = yy - ih + (.9 if ih > 3 else 0)
                ings.append(f"M{x:.1f} {yy:.1f} L{x + 1.4:.1f} {y1:.1f} H{x + w - 1.4:.1f} L{x + w:.1f} {yy:.1f} Z")
                caps.append(f"M{x + 1.4:.1f} {y1:.1f} H{x + w - 1.4:.1f} V{y1 + 1.2:.1f} H{x + 1.4:.1f} Z")
                yy -= ih
            val = f'<text class="val" x="{cx:.1f}" y="{y - 8:.1f}" text-anchor="middle">{t} · {e} runs</text>' if vals else ""
            s.append(f'<g class="d" tabindex="0" style="--i:{i}"><title>{short_day(d)}: {t} tasks closed, {e} evidence runs</title>'
                     f'<path class="ing{" peak" if t == peak else ""}" d="{"".join(ings)}"/><path class="cap" d="{"".join(caps)}"/>'
                     f'{val}</g>')
        elif t is not None:
            s.append(f'<g class="d" tabindex="0"><title>{short_day(d)}: 0 tasks closed, {e} evidence run{"" if e == 1 else "s"}</title>'
                     f'<circle cx="{cx:.1f}" cy="{base}" r="2.2" class="none"/></g>')
        else:
            s.append(f'<circle cx="{cx:.1f}" cy="{base}" r="1.6" class="none"/>')
        if i % step == 0:  # the first label sits inside the frame
            s.append(f'<text x="{max(0, cx - bw / 2) if i == 0 else cx:.1f}" y="{base + 22}" '
                     f'text-anchor="{"start" if i == 0 else "middle"}">{short_day(d)}</text>')
    s.append("</svg>")
    return "".join(s)


def ledger(stats):
    """Tasks closed each day as ingots, a wide and a narrow drawing, its caption and the table a screen reader gets."""
    days = _days(need(stats, "daily"))
    if not days:
        raise SystemExit("build.py: site/data/stats.json has no daily rows; run site/stats.py")
    peak = max(t for _, t, _ in days)
    n = (days[-1][0] - days[0][0]).days + 1
    where = max(days, key=lambda r: r[1])[0]
    rows = "".join(f"<tr><td>{long_day(d)}</td><td>{t}</td><td>{e}</td></tr>" for d, t, e in days)
    return (_ledger_svg(days, peak, 1200, "wide", max(1, math.ceil(n / 11)), True)
            + _ledger_svg(days, peak, 420, "narrow", max(1, math.ceil(n / 5)), False)
            + "\n<figcaption><span>Tasks closed per day, as ingots; a dot marks a day with none. Hover or focus a day for its "
              f"evidence runs.</span><span>Peak: {peak} on {long_day(where)}</span></figcaption>\n"
            + '<div class="vh"><table><caption>Tasks closed and evidence runs per working day</caption>'
              f"<tr><th>Day</th><th>Tasks</th><th>Evidence runs</th></tr>{rows}</table></div>")
