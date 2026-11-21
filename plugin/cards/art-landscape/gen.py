"""art-landscape: one landscape, dark and light, from the design tokens: a sky by the time of day, the sun or a crescent
moon, clouds or stars, then hills, mountains, dunes or sea in layers that darken from far to near. Seeded, so the same
slots draw the same bytes; clouds and waves drift and stars twinkle when motion is a duration."""
import math

from artkit import H, W, anim, art_main, el, glow, poly, ramp, seq, smooth
from panelkit import N, mix

# time: (sky-top accent, horizon accent, sun accent, sun x and y as shares of the frame)
LIGHT = {"dawn": (4, 2, 1, .24, .5), "day": (5, 0, 1, .64, .17), "dusk": (2, 1, 1, .74, .47),
         "night": (4, 4, None, .7, .2)}
MAX_SEA_LAYERS = 4  # more bands of waves run past works' 30 s capture limit (measured)
GROUND = {"hills": 3, "mountains": 4, "dunes": 1, "sea": 5}  # the accent each terrain is coloured from


def palette(s, T):
    dark = T.scheme == "dark"
    top_i, hor_i, sun_i = LIGHT[s["time"]][:3]
    pale = T.ink if dark else T.panel  # snow, cloud, star and moon: light on either scheme
    if s["time"] == "night":
        base = T.bg if dark else T.ink
        top, hor, sun = mix(base, T.a(top_i), .12), mix(base, T.a(hor_i), .4), pale
    else:
        k = .3 if dark else .45
        top, hor, sun = mix(T.bg, T.a(top_i), k), mix(T.bg, T.a(hor_i), k + .25), T.a(sun_i)
    near = mix(T.a(GROUND[s["terrain"]]), T.bg if dark else T.ink, .55 if dark else .45)
    return dict(dark=dark, pale=pale, top=top, hor=hor, sun=sun, near=near)


def layer_colours(n, c):
    """Far to near: each layer pulled from the horizon's haze toward the near ground."""
    return [mix(c["hor"], c["near"], (i + 1) / n) for i in range(n)]


def ridge(r, y, rough, levels=6):
    """A mountain line by midpoint displacement, each level half as rough as the last."""
    pts = [(-30, y + (r() - .5) * rough), (W + 30, y + (r() - .5) * rough)]
    d = rough
    for _ in range(levels):
        new = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            new += [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + (r() - .5) * d), b]
        pts, d = new, d * .55
    return pts


def sky(s, T, c, r):
    defs = ramp("sky", [(0, c["top"]), (.62, c["hor"])], 0, H, frame=True)
    out, used = [el("rect", width=W, height=H, fill="url(#sky)")], set()
    stars = 56 if s["time"] == "night" else 10 if s["time"] == "dawn" else 0
    for k in range(stars):
        tw = k % 4 == 0
        if tw:
            used.add("pulse")
        out.append(el("circle", anim("pulse", s["motion"], 1 / 8, None, r() * 3) if tw else "", cx=N(W * r()),
                      cy=N(H * .5 * r() ** 1.4), r=N(.6 + 1.1 * r()), fill=c["pale"],
                      opacity=N((.35 if s["time"] == "dawn" else .55) + .4 * r())))
    sx, sy = W * LIGHT[s["time"]][3], H * LIGHT[s["time"]][4]
    sr = 22 if s["time"] == "day" else 30
    defs += glow("halo", c["sun"], sx, sy, sr * 5, .5 if s["time"] != "night" else .25)
    used.add("pulse")
    out.append(el("circle", anim("pulse", s["motion"], 1 / 3), cx=N(sx), cy=N(sy), r=N(sr * 5), fill="url(#halo)"))
    out.append(el("circle", cx=N(sx), cy=N(sy), r=N(sr), fill=c["sun"]))
    if s["time"] == "night":  # a crescent: the sky itself, offset, bites the moon
        out.append(el("circle", cx=N(sx + sr * .45), cy=N(sy - sr * .25), r=N(sr * .9), fill="url(#sky)"))
    clouds = 0 if s["time"] == "night" else 2 + int(r() * 4)
    for _ in range(clouds):
        x, y, w = W * r(), H * (.07 + .26 * r()), 50 + 70 * r()
        puffs = [(x + w * (k / 4 - .5), y - w * .12 * math.sin(math.pi * k / 4) * (.6 + .6 * r()),
                  w * (.2 + .12 * r())) for k in range(5)]
        one = "".join(el("ellipse", cx=N(px + dx), cy=N(py), rx=N(pr), ry=N(pr * .55), fill=c["pale"])
                      for dx in (0, W) for px, py, pr in puffs)
        used.add("drift")
        out.append(f'<g opacity="{N(.16 if c["dark"] else .7)}"{anim("drift", s["motion"], 3 + 2 * r())}>{one}</g>')
    return "".join(out), defs, used, (sx, sy, sr)


def land(s, T, c, r, sun):
    n, terrain = s["layers"], s["terrain"]
    cols, out, defs, used = layer_colours(n, c), [], "", set()
    if terrain == "sea":
        hy = H * .6
        cx, w = W * (.12 + .3 * r()), W * (.2 + .25 * r())
        if abs(cx - sun[0]) < w * .6:
            cx = (cx + W * .55) % W
        pts = [(x, hy - H * (.08 + .07 * r()) * max(0, 1 - ((x - cx) / w) ** 2) ** .7) for x in range(-40, W + 41, 32)]
        out.append(el("path", d=smooth(pts, hy + 2), fill=cols[0]))
        defs += ramp("water", [(hy / H, mix(c["hor"], c["near"], .35)), (1, c["near"])], 0, H, frame=True)
        out.append(el("rect", y=N(hy), width=W, height=N(H - hy), fill="url(#water)"))
        sx, sy, sr = sun
        if sy < hy:  # the sun's path on the water
            for k in range(9):
                y = hy + 5 + k * k * 1.6 + k * 6
                half = sr * (1.3 - k * .07) * (.6 + .5 * r())
                used.add("pulse")
                out.append(el("line", anim("pulse", s["motion"], 1 / 6, None, r() * 4), x1=N(sx - half), y1=N(y),
                              x2=N(sx + half), y2=N(y), stroke=c["sun"], stroke_width=N(1.5 + k * .3),
                              stroke_linecap="round", opacity=N(.6 - k * .045)))
        n = min(n, MAX_SEA_LAYERS)  # each band of waves moves, so is repainted every frame
        for i in range(1, n):
            y0, y1 = hy + (H - hy) * (i - 1) / (n - 1), hy + (H - hy) * i / (n - 1)
            waves = []
            for _ in range(4 + 3 * i):
                x, y, w = W * r(), y0 + (y1 - y0) * r(), 10 + 8 * i + 10 * r()
                a = 1.5 + i
                waves += [f"M{N(x + dx)} {N(y)}q{N(w / 4)} {N(-a)} {N(w / 2)} 0t{N(w / 2)} 0" for dx in (0, W)]
            used.add("drift")
            out.append(f'<g{anim("drift", s["motion"], 4 - i * .4)}>'
                       + el("path", d="".join(waves), fill="none", stroke=c["pale"], stroke_width=N(.8 + .35 * i),
                            stroke_linecap="round", opacity=N(.22 + .08 * i)) + "</g>")
        return "".join(out), defs, used, f"a sea under a headland, {n - 1} bands of waves"
    for i in range(n):
        t = i / (n - 1)
        if terrain == "mountains":
            pts = ridge(r, H * (.38 + .4 * t), H * (.5 - .26 * t))
            d = poly(pts, H + 20)
            out.append(el("path", d=d, fill=cols[i]))
            if i < n - 1:
                line = min(y for _, y in pts) + 18 + 14 * r()
                defs += f'<clipPath id="snow{i}"><rect width="{W}" height="{N(line)}"/></clipPath>'
                out.append(el("path", d=d, fill=c["pale"], opacity=N(.8 - .1 * i), clip_path=f"url(#snow{i})"))
        else:
            dunes = terrain == "dunes"
            step = W / ((2 + 2 * r()) if dunes else (3 + 3 * r() + 2 * t))
            amp = H * ((.035 if dunes else .06) + .04 * r())
            xs = [-60 + step * k for k in range(int((W + 120) / step) + 2)]
            pts = [(x, H * (.5 + .36 * t) + amp * (2 * r() - 1)) for x in xs]
            out.append(el("path", d=smooth(pts, H + 20), fill=cols[i]))
            if dunes:  # the lit crest of each dune line
                out.append(el("path", d=smooth(pts), fill="none", stroke=c["sun"], stroke_width="1.5",
                              opacity=N(.5 - .06 * i)))
    return "".join(out), defs, used, f"{n} layers of {terrain}"


def draw(s, T):
    r = seq(s, "time", "terrain", "layers", "seed")
    c = palette(s, T)
    top, defs, used, sun = sky(s, T, c, r)
    ground, more, used2, what = land(s, T, c, r, sun)
    still = "still" if s["motion"] == "none" else f"looping every {s['motion']}"
    return top + ground, defs + more, used | used2, f"A landscape at {s['time']}: {what}, in the {s['style']} " \
        f"colours, {still}."


art_main(draw)
