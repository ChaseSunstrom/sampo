"""art-abstract: one abstract picture, dark and light, from the design tokens: rings round a focus, drifting ribbons,
a tile weave or a burst of rays. Seeded, so the same slots draw the same bytes; looping when motion is a duration."""
import math

from artkit import H, W, anim, art_main, el, glow, ramp, seq, smooth
from panelkit import N


def orbits(s, T, cs, r):
    """Rings round an off-centre focus, each with a dashed track and beads, the rings turning both ways."""
    cx, cy = W * (.38 + .24 * r()), H * (.36 + .28 * r())
    n, used, out = s["density"], set(), []
    step = max(W, H) * .95 / n
    defs = glow("core", cs[0], cx, cy, step * 2.6, .55)
    out.append(el("circle", cx=N(cx), cy=N(cy), r=N(step * 2.6), fill="url(#core)"))
    for k in range(n):
        rad = step * (k + .8)
        c = cs[k % 3]
        out.append(el("circle", cx=N(cx), cy=N(cy), r=N(rad), fill="none", stroke=T.line, stroke_width="1",
                      opacity=N(.35 + .4 * r())))
        beads = [el("circle", cx=N(cx + rad * math.cos(a)), cy=N(cy + rad * math.sin(a)), r=N(2 + 3.5 * r()), fill=c)
                 for a in (2 * math.pi * r() for _ in range(1 + int(r() * 3)))]
        a0, sweep = 2 * math.pi * r(), .5 + 1.6 * r()
        x0, y0 = cx + rad * math.cos(a0), cy + rad * math.sin(a0)
        x1, y1 = cx + rad * math.cos(a0 + sweep), cy + rad * math.sin(a0 + sweep)
        arc = (f'<path d="M{N(x0)} {N(y0)}A{N(rad)} {N(rad)} 0 {int(sweep > math.pi)} 1 {N(x1)} {N(y1)}" '
               f'fill="none" stroke="{c}" stroke-width="{N(1.5 + 2.5 * r())}" stroke-linecap="round"/>')
        # no stroke-dasharray on what turns: Chromium re-dashes every frame, and a dozen dashed arcs starve the
        # screenshot past its 30 s (measured: 50 s a render dashed, 3 s solid)
        cls = "spin" if k % 2 else "spinr"
        used.add(cls)
        out.append(f"<g{anim(cls, s['motion'], 1 + k * .35, (cx, cy), r() * 10)}>{arc}{''.join(beads)}</g>")
    out.append(el("circle", cx=N(cx), cy=N(cy), r=N(step * .35), fill=cs[0]))
    return "".join(out), defs, used, f"{n} rings turning round a bright core"


MAX_RIBBONS = 6  # a render of more than this runs past works' 30 s capture limit (measured)


def drift(s, T, cs, r):
    """Ribbons of waves crossing the frame, each drawn over two widths so a slide of one width loops seamlessly."""
    n, used, out = min(s["density"], MAX_RIBBONS), {"drift"}, []  # each moving ribbon is repainted every frame
    defs = ramp("haze", [(0, T.bg), (1, cs[0])])
    out.append(el("rect", width=W, height=H, fill="url(#haze)", opacity=".18"))
    for k in range(n):
        y0 = H * (k + .5 + (r() - .5) * .6) / n
        amp, waves, ph = 12 + 46 * r(), 1 + int(r() * 3), 2 * math.pi * r()
        pts = [(x, y0 + amp * math.sin(2 * math.pi * waves * x / W + ph)) for x in range(0, 2 * W + 1, 32)]
        width = N(1.5 + 14 * r() ** 2)
        ribbon = el("path", d=smooth(pts), fill="none", stroke=cs[k % 3], stroke_width=width, stroke_linecap="round",
                    opacity=N(.3 + .55 * r()))
        motes = "".join(el("circle", cx=N(x + dx), cy=N(y0 + amp * math.sin(2 * math.pi * waves * x / W + ph)
                                                         + (r() - .5) * 30), r=N(1.5 + 2 * r()), fill=T.ink,
                           opacity=".6")
                        for x in (W * r() for _ in range(2)) for dx in (0, W))
        out.append(f"<g{anim('drift', s['motion'], 1 + 1.5 * r(), None, 0)}>{ribbon}{motes}</g>")
    return "".join(out), defs, used, f"{n} ribbons of waves drifting sideways"


def grid(s, T, cs, r):
    """A Truchet weave: quarter arcs in every tile, coloured by a diagonal ramp, a light band sweeping over it."""
    n, used = s["density"], {"sweep", "pulse"}
    size = W / n
    rows = math.ceil(H / size)
    paths = {0: [], 1: []}
    pulses = []
    for j in range(rows):
        for i in range(n):
            x, y, h = i * size, j * size, size / 2
            if r() < .5:
                d = (f"M{N(x + h)} {N(y)}A{N(h)} {N(h)} 0 0 1 {N(x)} {N(y + h)}"
                     f"M{N(x + h)} {N(y + size)}A{N(h)} {N(h)} 0 0 1 {N(x + size)} {N(y + h)}")
            else:
                d = (f"M{N(x + h)} {N(y)}A{N(h)} {N(h)} 0 0 0 {N(x + size)} {N(y + h)}"
                     f"M{N(x)} {N(y + h)}A{N(h)} {N(h)} 0 0 0 {N(x + h)} {N(y + size)}")
            paths[(i + j) % 2].append(d)
            if r() < .06:
                pulses.append(el("circle", anim("pulse", s["motion"], 1 / 6, None, r() * 4), cx=N(x + h),
                                 cy=N(y + h), r=N(h * .32), fill=cs[2]))
    defs = ramp("weave", [(0, cs[0]), (.5, cs[1]), (1, cs[2])], W, H, frame=True)
    defs += ramp("band", [(0, T.ink, 0), (.5, T.ink, 1), (1, T.ink, 0)], 1, 0)
    sw = N(max(1.5, size * .14))
    body = "".join(el("path", d="".join(ps), fill="none", stroke="url(#weave)", stroke_width=sw,
                      stroke_linecap="round", opacity=op) for ps, op in ((paths[0], "1"), (paths[1], ".75")) if ps)
    band = el("rect", x="0", y="0", width="280", height=H, fill="url(#band)", opacity=".14")
    body += f"<g{anim('sweep', s['motion'])}>{band}</g>" if s["motion"] != "none" else \
        el("rect", x=N(W * r() * .6), y="0", width="280", height=H, fill="url(#band)", opacity=".14")
    return body + "".join(pulses), defs, used, f"a weave of {n} by {rows} tiles"


def burst(s, T, cs, r):
    """Rays from a core in groups, each group turning at its own pace, ends beaded, a glow under all."""
    cx, cy = W * (.35 + .3 * r()), H * (.35 + .3 * r())
    n, used, out = s["density"], set(), []
    defs = glow("bloom", cs[0], cx, cy, 260, .5)
    out.append(el("circle", cx=N(cx), cy=N(cy), r="260", fill="url(#bloom)"))
    for g in range(n):
        c, rays = cs[g % 3], []
        for _ in range(3):
            a = 2 * math.pi * r()
            r0, r1 = 26 + 30 * r(), 90 + 330 * r() ** 1.5
            x0, y0, x1, y1 = cx + r0 * math.cos(a), cy + r0 * math.sin(a), cx + r1 * math.cos(a), cy + r1 * math.sin(a)
            rays.append(el("line", x1=N(x0), y1=N(y0), x2=N(x1), y2=N(y1), stroke=c, stroke_width=N(1 + 2.5 * r()),
                           stroke_linecap="round", opacity=N(.45 + .5 * r())))
            rays.append(el("circle", cx=N(x1), cy=N(y1), r=N(2 + 3 * r()), fill=c))
        cls = "spin" if g % 2 else "spinr"
        used.add(cls)
        out.append(f"<g{anim(cls, s['motion'], 2 + g * .5, (cx, cy), r() * 20)}>{''.join(rays)}</g>")
    out.append(el("circle", cx=N(cx), cy=N(cy), r="40", fill="none", stroke=T.ink, stroke_width="1",
                  stroke_dasharray="2 6", opacity=".5"))
    out.append(el("circle", cx=N(cx), cy=N(cy), r="18", fill=cs[0]))
    return "".join(out), defs, used, f"{n * 3} rays bursting from a core"


COMPOSITIONS = {"orbits": orbits, "drift": drift, "grid": grid, "burst": burst}


def draw(s, T):
    r = seq(s, "composition", "density", "seed")
    cs = [T.a(s["accent"] + k) for k in range(3)]
    body, defs, used, what = COMPOSITIONS[s["composition"]](s, T, cs, r)
    still = "still" if s["motion"] == "none" else f"looping every {s['motion']}"
    return body, defs, used, f"Abstract art: {what}, in the {s['style']} colours, {still}."


art_main(draw)
