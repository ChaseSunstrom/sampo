"""The six stations, each a close view of the same forge, drawn in the mark's language, each with its own fire.

T-4868: GAUGES and LEDGER below are design-time sketches of the Record with a past day's numbers in them. They are
never written into the page: page.html's {{gauges}}, {{ledger}}, {{kinds}}, {{blocks}}, {{lanes}} and {{asof}} are filled from
site/data/stats.json by site/build.py:fill_record (T-4842), and build.py here leaves those keys alone."""
import math, random
from art import *
import hero
from hero import hammer, HAMMER_BOX, coals, STONE_L, STONE_M, STONE_R, BAND_T, BAND_B, RIVETS, FRONT_RUNES

def fig(vbw, vbh, art, overlays='', cls='', label='', tags=''):
    return (f'<div class="fig {cls}" style="aspect-ratio:{vbw}/{vbh}" role="img" aria-label="{label}">'
            f'<svg class="art" viewBox="0 0 {vbw} {vbh}" aria-hidden="true">{art}</svg>{overlays}{tags}</div>')

def rockbed(x0, x1, y, seed, depth=120, u=2.4):
    rng = random.Random(seed); s = []; x = x0
    while x < x1:
        w = rng.uniform(70, 140); h = rng.uniform(.5, 1) * depth
        P = [(x, y + depth), (x + rng.uniform(0, 12), y + depth - h), (x + w * .45, y + depth - h - rng.uniform(4, 18)), (x + w - rng.uniform(0, 10), y + depth - h + rng.uniform(0, 14)), (x + w + 6, y + depth)]
        s.append(stone([(P, rng.choice(['url(#sB2)', 'url(#sB4)']), 80)], rng.randrange(1 << 30), u=u, pits=8, cracks=1, chisel=4,
                       rims=[([P[1], P[2], P[3]], '#FFB070', .35, 1.4)]))
        x += w * .82
    return ''.join(s)

def hammer_ov(vbw, vbh, k, cls):
    """The hammer as its own composited layer; its keyframes live in CSS (translate in % of its own box)."""
    x, y, w, h = HAMMER_BOX
    st = f'left:{x * k / vbw * 100:.3f}%;top:{y * k / vbh * 100:.3f}%;width:{w * k / vbw * 100:.3f}%;height:{h * k / vbh * 100:.3f}%;transform-origin:{-x / w * 100:.3f}% {-y / h * 100:.3f}%'
    return f'<svg class="ov hm {cls}" viewBox="{x} {y} {w} {h}" style="{st}" aria-hidden="true">{hammer()}</svg>'

def sparks_ov(vbw, vbh, cx, cy, n, seed, cls, reach=1.0):
    rng = random.Random(seed); s = []
    for i in range(n):
        a = math.radians(rng.uniform(-172, -8)); v = rng.uniform(60, 190) * reach
        dx, dy = math.cos(a) * v, math.sin(a) * v + rng.uniform(10, 50) * reach
        L = rng.uniform(8, 20); c = rng.choice(['#EAF2FF', '#FFE9B0', '#FFC24D', '#FF9440', '#FF6A4A']) if rng.random() > .15 else rng.choice(['#45E0A0', '#BC8BFF', '#6E9BFF'])
        ang = math.degrees(math.atan2(dy, dx))
        s.append(f'<path d="M{f(-L)} 0 H0" transform="translate({f(cx)} {f(cy)}) rotate({ang:.0f})" stroke="{c}" stroke-width="{rng.uniform(1.2, 2.2):.1f}" stroke-linecap="round" style="--dx:{f(dx)}px;--dy:{f(dy)}px;--d:{rng.uniform(0, .06):.2f}s"/>')
    R = 210 * reach
    return ov(vbw, vbh, (cx - R, cy - R, 2 * R, 1.35 * R), f'<g class="spk">{"".join(s)}</g>', cls)

def flash_ov(vbw, vbh, cx, cy, r, cls, halo='haloB'):
    return ov(vbw, vbh, (cx - r, cy - r, 2 * r, 2 * r), f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r)}" fill="url(#{halo})"/>{star4(cx, cy, r * .55, "#F4F8FF")}<path d="M{f(cx - r)} {f(cy)} H{f(cx + r)}" stroke="#DCE8FF" stroke-width="1.4" opacity=".8"/>', cls)

def embers_ov(vbw, vbh, box, n, seed, cls='emb'):
    rng = random.Random(seed); x, y, w, h = box; s = []
    for i in range(n):
        ex = rng.uniform(x + w * .2, x + w * .8); ey = y + h - rng.uniform(0, 20)
        c = rng.choice(['#FFC24D', '#FF9440', '#FFE9B0', '#FF6A4A', '#45E0A0', '#BC8BFF'])
        s.append(f'<circle cx="{f(ex)}" cy="{f(ey)}" r="{rng.uniform(1.2, 2.4):.1f}" fill="{c}" style="--i:{i};--x:{rng.uniform(-40, 40):.0f}px;--t:{rng.uniform(3.5, 6):.1f}s"/>')
    return ov(vbw, vbh, box, f'<g class="emr">{"".join(s)}</g>', cls)

def flags(W, y0, seed):
    """A few flagstones in perspective and packed earth between them, lit warm from the runes above."""
    rng = random.Random(seed); VX = W / 2
    rows = [y0, y0 + 26, y0 + 60, y0 + 110]
    edge = lambda xb, y: VX + (xb - VX) * (.7 + .3 * (y - y0) / 110)
    s = [f'<path d="{poly_d([(edge(-88, y0 + 3), y0 + 3), (edge(W + 88, y0 + 3), y0 + 3), (edge(W + 88, y0 + 107), y0 + 107), (edge(-88, y0 + 107), y0 + 107)])}" class="jt"/>']   # the joints, in the flags' own perspective
    for i in range(len(rows) - 1):
        ya, yb = rows[i], rows[i + 1]; n = 4 + i; step = (W + 200) / n; xs = [-100 + k * step + rng.uniform(-12, 12) for k in range(n + 1)]
        at = lambda xb, y: VX + (xb - VX) * (.7 + .3 * (y - y0) / 110)
        for k in range(n):
            g = 3 + i; P = [(at(xs[k] + g, ya + g), ya + g), (at(xs[k + 1] - g, ya + g), ya + g), (at(xs[k + 1] - g, yb - g), yb - g), (at(xs[k] + g, yb - g), yb - g)]
            heat = max(0, 1 - abs((P[0][0] + P[1][0]) / 2 - VX) / (W * .6))
            s.append(stone([(P, rng.choice(['url(#sB1)', 'url(#sB2)', 'url(#sB4)']), 0)], rng.randrange(1 << 30), u=1.6, pits=5, cracks=0, chisel=2,
                           rims=[([P[0], P[1]], '#FFB070', .15 + .35 * heat, 1.2)]))
    return ''.join(s)

# ------------------------------------------------------------------------------------------------ How: the forge road
# One scene, one eye-level three-quarter camera: a road along the flank of a hill at night, from the mine at the left to the
# record house at the right, with the eight stations on it, each at the hero's scale and each lit by its own fire. Three
# layers (far ridges, the road, the near rocks) pan at different speeds; the camera stops on the station being read.
HOW_RUNES = [('fehu', 'blue'), ('ehwaz', 'gold'), ('othala', 'green'), ('raido', 'violet'), ('tiwaz', 'orange'), ('dagaz', 'white'), ('mannaz', 'green'), ('berkano', 'gold')]
RD_H = 600; RD_Y0, RD_VH = 170, 430; RD_X = [260 + i * 380 for i in range(8)]   # the frame shows y 170-600; each station's centre on the road
RD_VB = {'far': (-900, 3000, .5), 'mid': (-520, 3960, 1.0), 'near': (-700, 4800, 1.25)}   # (x0, width, parallax) per layer
RD_BACK, RD_FRONT = 456, 600                                   # the road's back edge and the frame's foot
RD_SRC = [(318, 362), (712, 378), (1112, 446), (1424, 318), (1712, 446), (2246, 352), (2626, 368), (2930, 402)]   # each station's fire

def _rd_ground(rng):
    """The road: packed earth with flagstones laid in rows that foreshorten toward the hill, grass and scree at its edges."""
    s = [f'<path d="M-520 {RD_BACK} H3440 V{RD_FRONT} H-520 Z" fill="url(#roadG)"/>']
    rows = [RD_BACK, 463, 473, 487, 506, 532, 566, 606]
    for r in range(len(rows) - 1):
        y0, y1 = rows[r], rows[r + 1]; x = -520 + rng.uniform(-40, 0); wmin, wmax = 34 + r * 12, 60 + r * 20
        while x < 3440:
            w = rng.uniform(wmin, wmax); g = 1.2 + r * .5
            P = [(x + g, y0 + g), (x + w - g + rng.uniform(-2, 2), y0 + g), (x + w - g + rng.uniform(-3, 3), y1 - g), (x + g + rng.uniform(-3, 3), y1 - g)]
            s.append(f'<path d="{poly_d(P)}" fill="{rng.choice(["url(#sB1)", "url(#sB2)", "url(#sB4)", "url(#sB3)"])}" opacity="{.75 + r * .035:.2f}"/>'
                     f'<path d="M{f(P[0][0])} {f(P[0][1])} L{f(P[1][0])} {f(P[1][1])}" stroke="#8FA6D6" stroke-opacity="{.08 + r * .02:.2f}"/>')
            x += w
    # grass and scree along the back edge, where the road meets the hill
    tufts = []
    for _ in range(140):
        x = rng.uniform(-520, 3440); y = RD_BACK + rng.uniform(-3, 4); h = rng.uniform(6, 16)
        tufts.append(''.join(f'M{f(x + k * 2.2)} {f(y)} q{f(rng.uniform(-3, 3))} {f(-h * .6)} {f(rng.uniform(-5, 5))} {f(-h)}' for k in range(3)))
    s.append(f'<path d="{"".join(tufts)}" fill="none" stroke="var(--grass)" stroke-width="1.2" stroke-linecap="round"/>')
    return ''.join(s)

def _rd_hill(rng):
    """The hill behind the road: its crest against the night, outcrops on its face, a dry-laid retaining wall, a flume."""
    crest = [(-520, 330), (-200, 262), (40, 230), (300, 214), (520, 262), (760, 300), (980, 244), (1220, 286), (1480, 238), (1700, 196),
             (1960, 232), (2200, 288), (2440, 262), (2700, 300), (2960, 252), (3200, 290), (3440, 330)]
    pts = []
    for (x0, y0), (x1, y1) in zip(crest, crest[1:]):
        for k in range(6):
            t = k / 6; pts.append((lerp(x0, x1, t), lerp(y0, y1, t) + rng.uniform(-7, 7)))
    pts.append(crest[-1])
    s = [f'<path d="M-520 {RD_BACK + 4} L{" L".join(f"{f(x)} {f(y)}" for x, y in pts)} L3440 {RD_BACK + 4} Z" fill="url(#hillG)"/>',
         f'<path d="M{" L".join(f"{f(x)} {f(y)}" for x, y in pts)}" fill="none" stroke="#8FA6D6" stroke-opacity=".28" stroke-width="1.6"/>']   # the crest takes the sky
    for _ in range(26):                                                                                          # outcrops on the face
        x = rng.uniform(-480, 3400); y = rng.uniform(300, 440); r = rng.uniform(16, 44)
        P = [(x + r * math.cos(t) * rng.uniform(.75, 1.1), y + r * .62 * math.sin(t) * rng.uniform(.75, 1.1)) for t in (k * math.tau / 7 for k in range(7))]
        s.append(f'<path d="{poly_d(P)}" fill="var(--hill-rock)"/><path d="M{f(P[3][0])} {f(P[3][1])} L{f(P[4][0])} {f(P[4][1])} L{f(P[5][0])} {f(P[5][1])}" fill="none" stroke="#8FA6D6" stroke-opacity=".2" stroke-width="1.2"/>')
    # the retaining wall behind stations III and IV, dry-laid
    wall = []
    for row, (y0, y1) in enumerate(((392, 410), (410, 428), (428, 444), (444, 458))):
        x = 880 + (row % 2) * 18
        while x < 1560:
            w = rng.uniform(34, 58); wall.append(poly_d([(x + 1.5, y0 + 1.2), (x + w - 1.5, y0 + 1.2 + rng.uniform(-1, 1)), (x + w - 1.5, y1 - 1.2), (x + 1.5, y1 - 1.2)])); x += w
    s.append(f'<path d="M880 392 H1560 V458 H880 Z" fill="var(--joint)"/><path d="{"".join(wall)}" fill="url(#sB2)"/>'
             f'<path d="M880 392 H1560" stroke="#8FA6D6" stroke-opacity=".3" stroke-width="1.4"/>')
    return ''.join(s)

def _rd_far():
    """Two ranges far off, the nearer one darker, a fog band between them and the hill."""
    rng = random.Random(77); x0, x1 = RD_VB['far'][0], RD_VB['far'][0] + RD_VB['far'][1]      # the whole layer, end to end
    def ridge(y, amp, step, seed_shift, fill, rim):
        x = x0; pts = []
        while x < x1 + step:
            pts.append((x, y - rng.uniform(0, amp) * (.6 + .4 * math.sin(x / 260 + seed_shift)))); x += rng.uniform(step * .6, step * 1.3)
        d = f'M{x0} 600 L{" L".join(f"{f(x)} {f(y_)}" for x, y_ in pts)} L{x1} 600 Z'
        return f'<path d="{d}" fill="{fill}"/><path d="M{" L".join(f"{f(x)} {f(y_)}" for x, y_ in pts)}" fill="none" stroke="{rim}" stroke-opacity=".35" stroke-width="1.4"/>'
    return (ridge(300, 120, 90, 0, 'var(--ridge-b)', '#8FA6D6') + ridge(352, 80, 70, 2, 'var(--ridge-m)', '#8FA6D6')
            + f'<path d="M{x0} 330 H{x1} V430 H{x0} Z" fill="url(#fogBand)"/>')

def _rd_near():
    """Out-of-focus rocks and grass at the foot of the frame, nearer than the road, so the camera sits in the scene."""
    rng = random.Random(91); s = []; x = -700
    while x < 4100:
        w = rng.uniform(70, 190); h = rng.uniform(16, 46); y = RD_FRONT + 6
        P = [(x, y), (x + w * .12, y - h * .7), (x + w * .45, y - h), (x + w * .8, y - h * .8), (x + w, y)]
        s.append(f'<path d="{poly_d(P)}" class="nrock"/><path d="M{f(P[1][0])} {f(P[1][1])} L{f(P[2][0])} {f(P[2][1])} L{f(P[3][0])} {f(P[3][1])}" fill="none" stroke="#FFB070" stroke-opacity=".12" stroke-width="2"/>')
        x += w + rng.uniform(80, 260)
    return ''.join(s)

def _pool(x, y, rx, ry, op=1):
    """A station's light on the ground: warm, falling off with distance (not a halo in the air)."""
    return f'<ellipse cx="{f(x)}" cy="{f(y)}" rx="{f(rx)}" ry="{f(ry)}" fill="url(#poolG)" opacity="{op}"/>'

def _lantern(x, y, lit=False):
    """A lantern on a hook: iron frame, horn panes, a small flame."""
    return (f'<path d="M{f(x)} {f(y - 22)} V{f(y - 16)}" stroke="#2A2C33" stroke-width="2"/>'
            f'<path d="M{f(x - 7)} {f(y - 16)} H{f(x + 7)} L{f(x + 9)} {f(y + 10)} H{f(x - 9)} Z" fill="{"#FFD27A" if lit else "#5A3E1E"}" opacity="{1 if lit else .85}"/>'
            f'<path d="M{f(x - 7)} {f(y - 16)} H{f(x + 7)} L{f(x + 9)} {f(y + 10)} H{f(x - 9)} Z M{f(x)} {f(y - 16)} V{f(y + 10)}" fill="none" stroke="#1A1B20" stroke-width="1.6"/>'
            f'<path d="M{f(x - 10)} {f(y - 18)} H{f(x + 10)} L{f(x + 6)} {f(y - 24)} H{f(x - 6)} Z M{f(x - 10)} {f(y + 10)} H{f(x + 10)} V{f(y + 13)} H{f(x - 10)} Z" fill="#2A2C33"/>'
            + (f'<path d="M{f(x)} {f(y + 4)} q-3 -6 0 -12 q3 6 0 12 Z" fill="#FFF4D8"/>' if lit else f'<circle cx="{f(x)}" cy="{f(y + 3)}" r="1.6" fill="#FF9A3A"/>'))

def _bloom(x, y, r=60):
    return f'<circle cx="{f(x)}" cy="{f(y)}" r="{r}" fill="url(#bloomG)"/>'

def _shadow(x, y, w, skew):
    """A prop's shadow cast down the road, away from its fire."""
    return f'<path d="M{f(x - w / 2)} {f(y)} L{f(x + w / 2)} {f(y)} L{f(x + w / 2 + skew)} {f(y + 22)} L{f(x - w / 2 + skew)} {f(y + 22)} Z" fill="#000" opacity=".35" filter="url(#gl6)"/>'

def _waystone(x, y, i):
    P = [(x - 14, y), (x - 15, y - 40), (x - 6, y - 50), (x + 10, y - 47), (x + 15, y)]
    rn, col = HOW_RUNES[i]
    return (stone([(P, 'url(#sM)', 88)], 300 + i, u=1.2, pits=5, cracks=0, chisel=3, rims=[(P[1:4], '#8FA6D6', .35, 1.2)])
            + rune_cut(rn, x - 8, y - 41, .48, col, groove=3.6, core=1.3, glow=.35))

# ---- the eight stations: each returns (base art, its light when read, the parts that move) at its centre x on the road
def _r_capture(cx, rng):
    # the mine's mouth in the hill, timbered; rails out onto the road; a laden ore tub on them; a lantern on the lintel
    b = (f'<path d="M{cx - 120} 470 L{cx - 112} 330 Q{cx - 30} 300 {cx + 60} 330 L{cx + 68} 470 Z" fill="#06060A"/>'
         f'<path d="M{cx - 108} 470 L{cx - 102} 338 Q{cx - 30} 312 {cx + 52} 338 L{cx + 58} 470 Z" fill="url(#adit)"/>'
         f'<path d="M{cx - 128} 470 L{cx - 120} 324 H{cx - 104} L{cx - 110} 470 Z M{cx + 52} 470 L{cx + 58} 324 H{cx + 74} L{cx + 70} 470 Z" fill="url(#wood)"/>'
         f'<path d="M{cx - 136} 316 H{cx + 82} V332 H{cx - 136} Z" fill="url(#woodTop)"/><path d="M{cx - 136} 316 H{cx + 82}" stroke="#C8A070" stroke-opacity=".35"/>'
         f'<path d="M{cx - 74} 470 L{cx - 120} 600 M{cx + 6} 470 L{cx + 40} 600" stroke="#4A4C54" stroke-width="4"/>'
         + ''.join(f'<path d="M{f(lerp(cx - 74, cx - 120, t) - 6)} {f(470 + 130 * t)} H{f(lerp(cx + 6, cx + 40, t) + 6)}" stroke="#3A2A1C" stroke-width="{2 + 7 * t:.1f}" opacity=".9"/>' for t in (.05, .2, .4, .65, .95)))
    tub = [(cx - 10, 412), (cx + 132, 404), (cx + 122, 478), (cx - 2, 482)]
    ore, fl = _chunks(rng, cx + 60, 400, 16, 60, 9, 17)
    b += (_shadow(cx + 70, 488, 150, 40)
          + f'<path d="{poly_d(tub)}" fill="url(#steelFlat)"/><path d="M{cx - 8} 426 L{cx + 130} 418 M{cx - 5} 462 L{cx + 125} 456" stroke="#1A1B20" stroke-width="3"/>'
          + f'<path d="M{cx - 10} 412 L{cx + 132} 404" stroke="#8FA6D6" stroke-opacity=".5" stroke-width="1.4"/>'
          + ''.join(f'<circle cx="{f(x)}" cy="{f(y)}" r="2" fill="#4A4C54"/>' for x, y in ((cx, 430), (cx + 120, 422), (cx + 2, 466), (cx + 118, 460)))
          + ore + f'<path d="{"".join(f"M{f(x - 1.4)} {f(y)} l1.4 -1.1 l1.4 1.1 l-1.4 1.1 Z" for x, y in fl[::2])}" fill="#A88A58" opacity=".6"/>'
          + ''.join(f'<circle cx="{x}" cy="486" r="13" fill="#1A1B20"/><circle cx="{x}" cy="486" r="13" fill="none" stroke="#5A5E68" stroke-width="2.4"/><circle cx="{x}" cy="486" r="3.4" fill="#4A4C54"/>' for x in (cx + 18, cx + 104))
          + _lantern(*RD_SRC[0]))
    l = (_bloom(*RD_SRC[0], 70) + _lantern(*RD_SRC[0], True)
         + f'<path d="M{cx - 10} 412 L{cx - 2} 482" stroke="#FFB070" stroke-opacity=".8" stroke-width="2.4"/><path d="M{cx - 10} 412 L{cx + 60} 408" stroke="#FFC890" stroke-opacity=".6" stroke-width="1.6"/>'
         f'<g class="tw">{"".join(star4(x, y, 5 + (k % 3) * 1.5, "#FFE7A8") for k, (x, y) in enumerate(fl[::3]))}</g>')
    return b, l, ''

def _r_expand(cx, rng):
    # a brass balance on a dressed stone pillar, weights stacked at its foot, a lantern hung on its side
    py = 268
    b = (_shadow(cx, 494, 120, 46)
         + stone([([(cx - 34, 494), (cx - 30, 400), (cx + 30, 398), (cx + 34, 494)], 'url(#sM)', 90), ([(cx - 40, 404), (cx + 40, 400), (cx + 36, 388), (cx - 36, 392)], 'url(#sT)', 0)], 61, u=1.4, pits=12, cracks=1, chisel=6,
                 rims=[([(cx - 34, 494), (cx - 30, 400)], '#8FA6D6', .3, 1.4)])
         + f'<path d="M{cx - 4} 392 H{cx + 4} V{py + 6} H{cx - 4} Z" fill="url(#brass)"/><path d="M{cx - 4} 392 V{py + 6}" stroke="#FFF1CC" stroke-opacity=".4"/>'
         + f'<path d="M{cx - 10} {py - 4} L{cx} {py - 22} L{cx + 10} {py - 4} Z" fill="url(#brass)"/>'
         + ''.join(f'<path d="M{cx + 44 + k * 4} {494 - k * 12} h{24 - k * 5} v-10 h-{24 - k * 5} Z" fill="url(#brass)"/>' for k in range(3))
         + f'<path d="M{cx + 34} 360 H{cx + 58}" stroke="#2A2C33" stroke-width="2.4"/>' + _lantern(cx + 58, 382))
    pan = lambda x, y, load: (f'<path d="M{x} {py} L{x - 30} {y} M{x} {py} L{x + 30} {y} M{x} {py} V{y}" stroke="#A88A5A" stroke-opacity=".8" stroke-width="1.2"/>'
                              f'<path d="M{x - 36} {y} Q{x} {y + 18} {x + 36} {y} Z" fill="url(#brass)"/><path d="M{x - 36} {y} H{x + 36}" stroke="#FFF1CC" stroke-opacity=".45"/>{load}')
    ore, _ = _chunks(rng, cx - 96, 336, 3, 12, 8, 11)
    weight = f'<path d="M{cx + 80} 340 V326 Q{cx + 96} 318 {cx + 112} 326 V340 Z" fill="url(#brass)"/>'
    m = (f'<g class="bm"><path d="M{cx - 100} {py} H{cx + 100}" stroke="url(#brass)" stroke-width="6" stroke-linecap="round"/><circle cx="{cx}" cy="{py}" r="7" fill="#E9C98A"/><circle cx="{cx}" cy="{py}" r="2.6" fill="#6A4A20"/></g>'
         f'<g class="pl">{pan(cx - 96, 342, ore)}</g><g class="pr">{pan(cx + 96, 342, weight)}</g>')
    l = (_bloom(cx + 58, 382, 64) + _lantern(cx + 58, 382, True)
         + f'<path d="M{cx - 100} {py - 3} H{cx + 100}" stroke="#FFF4D8" stroke-opacity=".75" stroke-width="1.6"/>'
         + f'<path d="M{cx + 30} 398 L{cx + 34} 494" stroke="#FFB070" stroke-opacity=".7" stroke-width="2"/>' + star4(cx, py - 22, 9, '#FFF4D8'))
    HOW_PIV.append(f'.how .mv1 .bm{{transform-origin:{cx}px {py}px}}')
    return b, l, m

def _r_ground(cx, rng):
    # bedrock breaking through the road: the assayer's table against it, the touchstone, the needles, a copper brazier
    rock = [(cx - 150, 470), (cx - 140, 380), (cx - 96, 336), (cx - 30, 330), (cx + 10, 360), (cx + 20, 470)]
    b = (stone([(rock, 'url(#sL)', 70)], 71, u=2, pits=30, cracks=4, chisel=14, rims=[(rock[1:5], '#8FA6D6', .35, 1.6)])
         + _shadow(cx + 10, 494, 200, 50)
         + stone([([(cx - 90, 494), (cx - 86, 440), (cx + 90, 436), (cx + 94, 494)], 'url(#sB2)', 90), ([(cx - 100, 444), (cx + 96, 440), (cx + 84, 426), (cx - 90, 430)], 'url(#sT)', 0)], 72, u=1.4, pits=10, cracks=1, chisel=5,
                 rims=[([(cx - 90, 430), (cx + 84, 426)], '#8FA6D6', .35, 1.2)]))
    T = [(cx - 70, 430), (cx + 20, 424), (cx + 30, 432), (cx - 62, 438)]
    b += (f'<path d="{poly_d(T)}" fill="#0A0B10"/><path d="M{cx - 70} 430 L{cx + 20} 424 L{cx + 30} 432" fill="none" stroke="#6A7080" stroke-opacity=".6"/>'
          + ''.join(f'<path d="M{cx - 58 + k * 13} 434 L{cx - 50 + k * 13} 427" stroke="{c}" stroke-width="2.6" stroke-linecap="round" opacity=".8"/>' for k, c in enumerate(('#D8D8E0', '#E9C98A', '#C8A060', '#B8B8C4')))
          + f'<path d="M{cx - 4} 433.5 L{cx + 14} 426.5" stroke="#C8A86A" stroke-width="2.8" stroke-linecap="round" opacity=".25"/>'
          + f'<path d="M{cx + 40} 428 H{cx + 70} V434 H{cx + 40} Z" fill="url(#wood)"/>'
          + ''.join(f'<path d="M{cx + 44 + k * 5} 430 L{cx + 36 + k * 7} {372 + abs(k - 2) * 5}" stroke="#B8B0A0" stroke-width="2" stroke-linecap="round"/><circle cx="{cx + 36 + k * 7}" cy="{372 + abs(k - 2) * 5}" r="2.4" fill="{("#E9C98A", "#D8D8E0", "#C8A060", "#E9C98A", "#B8B8C4")[k]}"/>' for k in range(5)))
    bx, by = RD_SRC[2]
    b += (f'<path d="M{bx - 22} {by} H{bx + 22} L{bx + 14} {by + 18} H{bx - 14} Z" fill="#B0622E"/><path d="M{bx - 22} {by} H{bx + 22}" stroke="#FFC890" stroke-opacity=".5"/>'
          f'<path d="M{bx - 10} {by + 18} L{bx - 16} 494 M{bx + 10} {by + 18} L{bx + 16} 494" stroke="#2A2C33" stroke-width="3"/>'
          f'<path d="M{bx - 16} {by} q8 -6 16 -2 q8 -6 16 2 Z" fill="#3A1A10"/><path d="M{bx - 12} {by - 1} q6 -3 12 -1 q6 -3 12 1" stroke="#FF7A2A" stroke-width="2" fill="none" opacity=".8"/>')
    l = (_bloom(bx, by - 10, 70)
         + f'<g>{"".join(f"""<path d="{tongue(bx + dx, by, w, h, .3, ph, .8)}" fill="url(#fOut)"/><path d="{tongue(bx + dx, by, w * .5, h * .55, .4, ph + .6, .6)}" fill="url(#fMid)"/>""" for dx, w, h, ph in ((0, 9, 34, 0), (-9, 6, 20, 1.6), (9, 6, 22, 3)))}</g>'
         + f'<path class="ink" d="M{cx - 4} 433.5 L{cx + 14} 426.5" pathLength="100" stroke="#FFD27A" stroke-width="3" stroke-linecap="round"/>'
         + f'<path d="M{cx - 4} 433.5 L{cx + 14} 426.5" stroke="#FF9A3A" stroke-width="9" stroke-linecap="round" opacity=".25"/>'
         + f'<path d="M{cx + 84} 426 L{cx + 90} 436 L{cx + 94} 494" fill="none" stroke="#FFB070" stroke-opacity=".7" stroke-width="2"/>'
         + f'<path d="M{rock[3][0]} {rock[3][1]} L{rock[4][0]} {rock[4][1]} L{rock[5][0]} {rock[5][1]}" fill="none" stroke="#FFB070" stroke-opacity=".5" stroke-width="2"/>')
    return b, l, ''

def _r_plan(cx, rng):
    # a lean-to against the wall: two posts, a plank roof, a slate on a trestle with the plan chalked on it, a lamp hung inside
    b = (_shadow(cx, 494, 230, 50)
         + f'<path d="M{cx - 120} 494 L{cx - 116} 292 H{cx - 104} L{cx - 106} 494 Z M{cx + 104} 494 L{cx + 108} 300 H{cx + 120} L{cx + 118} 494 Z" fill="url(#wood)"/>'
         + f'<path d="M{cx - 140} 300 L{cx + 140} 280 L{cx + 140} 292 L{cx - 140} 312 Z" fill="url(#woodTop)"/>'
         + ''.join(f'<path d="M{cx - 140 + k * 40} {300 - k * 2.86} L{cx - 140 + k * 40} {312 - k * 2.86}" stroke="#1A0E08" stroke-opacity=".5"/>' for k in range(8))
         + f'<path d="M{cx - 140} 300 L{cx + 140} 280" stroke="#C8A070" stroke-opacity=".4" stroke-width="1.4"/>'
         + f'<path d="M{cx - 80} 494 L{cx - 62} 430 M{cx - 40} 494 L{cx - 58} 430 M{cx + 80} 494 L{cx + 62} 430 M{cx + 40} 494 L{cx + 58} 430" stroke="#4A3220" stroke-width="5" stroke-linecap="round"/>')
    Bd = [(cx - 96, 336), (cx + 92, 330), (cx + 98, 438), (cx - 92, 442)]
    b += (f'<path d="{poly_d(Bd)}" fill="#5A3E26"/><path d="{poly_d([(cx - 88, 344), (cx + 84, 338), (cx + 89, 430), (cx - 84, 434)])}" fill="#14161C"/>'
          f'<path d="M{cx - 96} 336 L{cx + 92} 330" stroke="#C8A070" stroke-opacity=".45" stroke-width="1.4"/>')
    chalk = (f'M{cx - 70} 378 L{cx + 30} 374 L{cx + 58} 382 L{cx + 30} 390 L{cx - 70} 392 Z'
             f'M{cx - 70} 404 L{cx + 58} 400 M{cx - 70} 399 V409 M{cx + 58} 395 V405 M{cx - 40} 382 V388 M{cx - 10} 381 V387'
             f'M{cx - 70} 418 h40 M{cx - 70} 424 h28')
    steps = ''.join(f'M{cx - 64 + k * 30} {356 - k * .6} m-5 0 a5 5 0 1 0 10 0 a5 5 0 1 0 -10 0' for k in range(4))
    b += (f'<path d="{chalk}{steps}" fill="none" stroke="#E6E2D8" stroke-opacity=".3" stroke-width="1.4"/>'
          f'<path d="M{cx - 30} 440 L{cx + 6} 431 M{cx - 30} 440 L{cx + 10} 440" stroke="#B8C0D0" stroke-width="2" stroke-linecap="round"/>'
          + f'<path d="M{RD_SRC[3][0]} 290 V{RD_SRC[3][1] - 22}" stroke="#2A2C33" stroke-width="1.6"/>' + _lantern(*RD_SRC[3]))
    l = (_bloom(*RD_SRC[3], 70) + _lantern(*RD_SRC[3], True)
         + f'<path class="ink" d="{chalk}" pathLength="100" fill="none" stroke="#F8F4EA" stroke-width="1.8"/>'
         + f'<path class="ink" d="{steps}" pathLength="100" fill="none" stroke="#FFD27A" stroke-width="1.8"/>'
         + f'<path d="M{cx - 96} 336 L{cx + 92} 330" stroke="#FFC890" stroke-opacity=".7" stroke-width="2"/><path d="M{cx - 140} 312 L{cx + 140} 292" stroke="#FFB070" stroke-opacity=".5" stroke-width="2"/>')
    return b, l, ''

def _r_execute(cx, rng):
    # the helve hammer: a water wheel fed by a flume off the hill turns a cam that lifts the helve; the head falls on the anvil
    k = .62; ax, ay = cx - 150 - 139 * k + 60, 470 - 146 * k                 # the anvil on its stump
    face = ay + 86 * k; bar_t = face - 7
    b = (_shadow(cx - 60, 494, 260, 50)
         + f'<path d="M{cx - 150} 494 L{cx - 146} 470 L{cx - 40} 470 L{cx - 36} 494 Z" fill="url(#wood)"/><ellipse cx="{cx - 93}" cy="470" rx="54" ry="7" fill="url(#woodTop)"/>'
         + f'<g transform="translate({f(ax)} {f(ay)}) scale({k})">{anvil(seed=9, mark=False, detail=True)}</g>'
         + f'<path d="M{cx - 130} {f(bar_t)} H{cx - 64} V{f(face)} H{cx - 130} Z" fill="url(#billetW)"/>')
    # the flume and the wheel
    wx, wy, R = cx + 96, 400, 74
    b += (f'<path d="M{cx + 40} 250 L{cx + 150} 310 L{cx + 150} 322 L{cx + 40} 262 Z" fill="url(#wood)"/><path d="M{cx + 40} 252 L{cx + 150} 312" stroke="#7C9AC8" stroke-width="3" opacity=".6"/>'
          f'<path d="M{cx + 46} 262 L{cx + 50} 494 M{cx + 140} 318 L{cx + 144} 494" stroke="#3A2A1C" stroke-width="5"/>'
          f'<circle cx="{wx}" cy="{wy}" r="{R}" fill="none" stroke="url(#wood)" stroke-width="10"/><circle cx="{wx}" cy="{wy}" r="{R - 18}" fill="none" stroke="#3A2A1C" stroke-width="3"/>')
    pvx, pvy, hx = cx - 4, 362, cx - 98
    b += (f'<path d="M{pvx - 10} 494 L{pvx - 8} {pvy - 10} H{pvx + 10} L{pvx + 12} 494 Z" fill="url(#wood)"/><path d="M{pvx - 8} {pvy - 10} V494" stroke="#C8A070" stroke-opacity=".3"/>'
          f'<path d="M{pvx - 30} 494 L{pvx - 8} 420 M{pvx + 32} 494 L{pvx + 10} 420" stroke="#3A2A1C" stroke-width="5"/>')
    spokes = ''.join(f'M{f(wx + math.cos(t) * 10)} {f(wy + math.sin(t) * 10)} L{f(wx + math.cos(t) * (R - 4))} {f(wy + math.sin(t) * (R - 4))}' for t in (q * math.tau / 8 for q in range(8)))
    paddles = ''.join(f'M{f(wx + math.cos(t) * (R - 2))} {f(wy + math.sin(t) * (R - 2))} L{f(wx + math.cos(t) * (R + 12))} {f(wy + math.sin(t) * (R + 12))}' for t in (q * math.tau / 16 for q in range(16)))
    m = (f'<g class="ww"><path d="{spokes}" stroke="#4A3220" stroke-width="5"/><path d="{paddles}" stroke="#5A3E26" stroke-width="7"/><circle cx="{wx}" cy="{wy}" r="12" fill="#2A2C33"/></g>'
         f'<g class="hv"><path d="M{hx - 4} {pvy - 6} L{cx + 40} {pvy - 4} L{cx + 40} {pvy + 6} L{hx - 4} {pvy + 6} Z" fill="url(#wood)"/>'
         f'<path d="M{hx - 20} {pvy - 14} H{hx + 18} L{hx + 16} {f(bar_t)} H{hx - 18} Z" fill="url(#steelFlat)"/><path d="M{hx - 20} {pvy - 14} L{hx - 18} {f(bar_t)}" stroke="#8FA6D6" stroke-opacity=".5" stroke-width="1.4"/>'
         f'<path d="M{hx - 22} {pvy - 2} H{hx + 20} V{pvy + 4} H{hx - 22} Z" fill="#2A2C33"/><circle cx="{pvx}" cy="{pvy}" r="5" fill="#4A4C54"/></g>')
    l = (_bloom(cx - 97, face, 80)
         + f'<path d="M{cx - 130} {f(bar_t)} H{cx - 64} V{f(face)} H{cx - 130} Z" fill="url(#billetH)"/>'
         + f'<ellipse cx="{cx - 97}" cy="{f(face + 2)}" rx="58" ry="10" fill="url(#faceGlow)"/>'
         + f'<g class="burst">{_spray(hx, bar_t, 22, 77, -175, -5, 14, 70)}</g>'
         + f'<path d="M{cx - 150} 494 L{cx - 146} 470" stroke="#FFB070" stroke-opacity=".7" stroke-width="2"/>')
    HOW_PIV.append(f'.how .mv4 .hv{{transform-origin:{pvx}px {pvy}px}} .how .mv4 .ww{{transform-origin:{wx}px {wy}px}}')
    return b, l, m

def _r_verify(cx, rng):
    # the flume's tail fills a long stone trough; the tongs hold a bar into it at an angle, its wet half dark and bent
    tr = [(cx - 120, 448), (cx + 110, 444), (cx + 116, 494), (cx - 124, 494)]
    b = (_shadow(cx, 494, 250, 46)
         + stone([(tr, 'url(#sB2)', 84)], 91, u=1.6, pits=16, cracks=2, chisel=6, rims=[(tr[:2], '#8FA6D6', .45, 1.4)])
         + f'<path d="M{cx - 116} 450 L{cx + 106} 446 L{cx + 100} 458 L{cx - 110} 462 Z" fill="#4A4550"/>'
         + f'<path d="M{cx - 108} 452 L{cx + 98} 448 L{cx + 94} 456 L{cx - 104} 459 Z" fill="url(#waterR)"/>'
         + f'<path d="M{cx - 260} 300 L{cx - 120} 420 L{cx - 116} 430 L{cx - 262} 312 Z" fill="url(#wood)"/><path d="M{cx - 120} 424 q4 10 2 26" stroke="#7C9AC8" stroke-width="3" fill="none" opacity=".6"/>')
    # the tongs from the right, the bar going in at the waterline
    b += (f'<path d="M{cx + 132} 494 L{cx + 150} 424 L{cx + 168} 494 M{cx + 140} 452 H{cx + 160}" stroke="#4A3220" stroke-width="5" fill="none" stroke-linecap="round"/>'   # a trestle the reins rest on
          f'<path d="M{cx + 196} 410 L{cx + 14} 444 M{cx + 198} 418 L{cx + 20} 448" stroke="#26282E" stroke-width="5" stroke-linecap="round"/>'
          f'<path d="M{cx + 196} 410 L{cx + 14} 444" stroke="#A8B0C0" stroke-opacity=".3" stroke-width="1.2"/>'
          f'<path d="M{cx + 22} 440 L{cx - 18} 452" stroke="#5A2A1C" stroke-width="9" stroke-linecap="round"/>'
          f'<path d="M{cx - 18} 452 L{cx - 34} 458" stroke="#1A1416" stroke-width="8" stroke-linecap="round" opacity=".7"/>'
          + _lantern(*RD_SRC[5]) + f'<path d="M{RD_SRC[5][0]} {RD_SRC[5][1] - 22} V300 M{RD_SRC[5][0] - 4} 300 L{RD_SRC[5][0] + 4} 494" stroke="#3A2A1C" stroke-width="4"/>')
    l = (_bloom(*RD_SRC[5], 60) + _lantern(*RD_SRC[5], True)
         + f'<path d="M{cx + 22} 440 L{cx - 14} 451" stroke="#FF8A2A" stroke-width="9" stroke-linecap="round"/><ellipse cx="{cx - 14}" cy="452" rx="26" ry="5" fill="url(#faceGlow)"/>'
         + ''.join(f'<ellipse cx="{cx - 14}" cy="452.6" rx="{r}" ry="{f(r * .14)}" fill="none" stroke="#C8D8F0" stroke-opacity=".55" stroke-width="1" class="rip"/>' for r in (14, 26, 40))
         + f'<g class="stm">{wisp(cx - 20, 446, 70, 150, 93, "vapor", .85)}{wisp(cx + 6, 444, 56, 120, 94, "vapor", .65)}</g>'
         + f'<path d="M{cx - 116} 450 L{cx + 106} 446" stroke="#FFC890" stroke-opacity=".55" stroke-width="1.4"/>')
    return b, l, ''

def _r_reflect(cx, rng):
    # a big grindstone in a timber frame over its water trough, a treadle, a bar held to the stone; a lantern on a post
    wx, wy, R = cx - 10, 392, 72
    b = (_shadow(cx, 494, 220, 48)
         + f'<path d="M{cx - 90} 494 L{cx - 86} 452 L{cx + 70} 452 L{cx + 74} 494 Z" fill="url(#wood)"/><path d="M{cx - 82} 452 L{cx + 66} 452 L{cx + 62} 462 L{cx - 78} 462 Z" fill="url(#waterR)"/>'
         + f'<path d="M{wx - 64} 494 L{wx - 6} {wy} M{wx + 64} 494 L{wx + 6} {wy}" stroke="#4A3220" stroke-width="7" stroke-linecap="round"/>'
         + f'<circle cx="{wx}" cy="{wy}" r="{R}" fill="url(#sB2)"/><circle cx="{wx}" cy="{wy}" r="{R}" fill="none" stroke="#8A7F86" stroke-width="3"/>'
         + f'<circle cx="{wx}" cy="{wy}" r="{R - 8}" fill="none" stroke="#000" stroke-opacity=".2" stroke-width="1.4"/>'
         + f'<path d="M{wx} {wy} L{wx + 40} {wy - 26} L{wx + 40} {wy - 46}" stroke="#3A3C44" stroke-width="5" stroke-linecap="round" fill="none"/>'
         + f'<path d="M{wx - R + 6} {wy - 30} L{wx - R - 70} {wy - 66}" stroke="#A8B0C0" stroke-width="7" stroke-linecap="round"/>'
         + f'<path d="M{RD_SRC[6][0]} 494 L{RD_SRC[6][0]} {RD_SRC[6][1] - 24} H{RD_SRC[6][0] - 20}" stroke="#3A2A1C" stroke-width="5" fill="none"/>' + _lantern(RD_SRC[6][0] - 20, RD_SRC[6][1]))
    m = (f'<g class="wh"><path d="' + ''.join(f'M{f(wx + math.cos(t) * 16)} {f(wy + math.sin(t) * 16)} L{f(wx + math.cos(t) * (R - 6))} {f(wy + math.sin(t) * (R - 6))}' for t in (q * math.tau / 9 + .3 for q in range(9)))
         + f'" stroke="#000" stroke-opacity=".2" stroke-width="2"/><circle cx="{wx}" cy="{wy}" r="10" fill="#4A4C54"/>'
         f'<path d="M{wx - R + 5} {wy} A{R - 5} {R - 5} 0 0 1 {wx} {wy - R + 5}" fill="none" stroke="#C8B8A8" stroke-opacity=".4" stroke-width="2"/></g>')
    l = (_bloom(RD_SRC[6][0] - 20, RD_SRC[6][1], 60) + _lantern(RD_SRC[6][0] - 20, RD_SRC[6][1], True)
         + f'<path d="M{wx + R * .7} {wy - R * .7} A{R} {R} 0 0 1 {wx + R} {wy}" fill="none" stroke="#FFB070" stroke-opacity=".7" stroke-width="2.6"/>'
         + f'<g class="burst">{_spray(wx - R + 6, wy - 30, 20, 79, -230, -120, 12, 66)}</g>')
    HOW_PIV.append(f'.how .mv6 .wh{{transform-origin:{wx}px {wy}px}}')
    return b, l, m

def _r_record(cx, rng):
    # the record house: a small stone hut, its doorway lit; before it a stamping block, the finished bar, the punch; a rack of work
    hut = [(cx - 40, 494), (cx - 40, 360), (cx + 70, 330), (cx + 180, 360), (cx + 180, 494)]
    b = (stone([(hut, 'url(#sM)', 90)], 121, u=1.6, pits=24, cracks=3, chisel=10, rims=[(hut[1:4], '#8FA6D6', .35, 1.6)])
         + f'<path d="M{cx - 52} 362 L{cx + 70} 322 L{cx + 192} 362 L{cx + 186} 368 L{cx + 70} 332 L{cx - 46} 368 Z" fill="#2A2630"/>'
         + f'<path d="M{cx + 46} 494 V410 Q{cx + 70} 390 {cx + 94} 410 V494 Z" fill="#1A0E08"/>'
         + ''.join(f'<path d="M{cx + 120 + q * 12} 494 L{cx + 132 + q * 14} {414 + q * 6}" stroke="url(#temper{q % 4})" stroke-width="7" stroke-linecap="square"/>' for q in range(4)))
    b += (_shadow(cx - 70, 494, 140, -40)
          + stone([([(cx - 120, 494), (cx - 116, 456), (cx - 20, 452), (cx - 16, 494)], 'url(#sB2)', 90), ([(cx - 124, 460), (cx - 14, 456), (cx - 22, 446), (cx - 116, 450)], 'url(#sT)', 0)], 122, u=1.2, pits=8, cracks=1, chisel=4)
          + f'<path d="M{cx - 112} 446 L{cx - 34} 442 L{cx - 22} 446.5 L{cx - 34} 451 L{cx - 112} 452 Z" fill="url(#temper1)"/><path d="M{cx - 112} 446 L{cx - 34} 442" stroke="#C8D4F0" stroke-opacity=".5"/>'
          + f'<path d="M{cx - 76} 448 m-5 -3 h10 v5 l-5 3 l-5 -3 Z" fill="#0A0B0F" opacity=".7"/>')
    b += (f'<path d="M{cx - 112} 452 V350 H{cx - 102} V452 Z M{cx - 50} 452 V350 H{cx - 40} V452 Z" fill="url(#steelFlat)"/>'          # the press frame
          f'<path d="M{cx - 118} 342 H{cx - 34} V356 H{cx - 118} Z" fill="#2A2C33"/><path d="M{cx - 118} 342 H{cx - 34}" stroke="#8FA6D6" stroke-opacity=".5" stroke-width="1.2"/>'
          f'<path d="M{cx - 112} 350 V452" stroke="#8FA6D6" stroke-opacity=".35"/><circle cx="{cx - 76}" cy="349" r="6" fill="#4A4C54"/>')
    m = (f'<g class="pn"><path d="M{cx - 82} 360 H{cx - 70} V430 L{cx - 72} 440 H{cx - 80} L{cx - 82} 430 Z" fill="url(#steelFlat)"/>'
         f'<path d="M{cx - 86} 360 Q{cx - 76} 348 {cx - 66} 360 Z" fill="#4A515E"/><path d="M{cx - 82} 362 V430" stroke="#C8D4F0" stroke-opacity=".5" stroke-width="1.2"/></g>')
    l = (f'<path d="M{cx + 46} 494 V410 Q{cx + 70} 390 {cx + 94} 410 V494 Z" fill="url(#doorLit)"/>'
         + f'<path d="M{cx + 50} 494 L{cx - 10} 560 L{cx + 150} 560 L{cx + 90} 494 Z" fill="url(#doorCast)"/>'
         + f'<path d="M{cx - 76} 448 m-5 -3 h10 v5 l-5 3 l-5 -3 Z" fill="#FFD27A"/>{star4(cx - 76, 447, 14, "#FFF4D8")}'
         + f'<path d="M{cx - 112} 446 L{cx - 34} 442" stroke="#FFE7A8" stroke-opacity=".7" stroke-width="1.4"/>')
    return b, l, m

RD_POOL = [(290, 500, 210, 50), (660, 500, 190, 46), (1072, 496, 200, 46), (1410, 498, 210, 48), (1684, 500, 220, 50), (2190, 498, 200, 46), (2520, 498, 200, 46), (2950, 500, 230, 50)]   # each station's light on the road
HOW_DRAW = [_r_capture, _r_expand, _r_ground, _r_plan, _r_execute, _r_verify, _r_reflect, _r_record]
HOW_PIV = []

def how():
    """The forge road, as three layers in one frame, and the generated CSS that moves the camera from station to station."""
    rng = random.Random(31); HOW_PIV.clear()
    base, pool, lit, mv, way = [], [], [], [], []
    base.append(_rd_hill(rng)); base.append(_rd_ground(rng))
    for i, cx in enumerate(RD_X):
        b, l, m = HOW_DRAW[i](cx, rng)
        base.append(b); way.append(_waystone(cx - 150, 548, i))
        pool.append(f'<g class="hs" data-i="{i}">{_pool(*RD_POOL[i])}</g>')                     # its light on the road, under everything standing on it
        rg = rune_glow(HOW_RUNES[i][0], cx - 158, 507, .48, '#FFE7A8', w=2.2, gid='gl3', crawl=False)
        lit.append(f'<g class="hs" data-i="{i}">{l}{rg}</g>'); mv.append(f'<g class="mv mv{i}">{m}</g>' if m else '')
    base.insert(2, ''.join(pool))
    vb = lambda k: f'{RD_VB[k][0]} {RD_Y0} {RD_VB[k][1]} {RD_VH}'
    layers = (f'<svg class="pan far" viewBox="{vb("far")}" preserveAspectRatio="xMinYMid meet" aria-hidden="true">{_rd_far()}</svg>'
              f'<svg class="pan mid" viewBox="{vb("mid")}" preserveAspectRatio="xMinYMid meet" aria-hidden="true">{"".join(base)}{"".join(mv)}{"".join(way)}{"".join(lit)}</svg>'
              f'<svg class="pan near" viewBox="{vb("near")}" preserveAspectRatio="xMinYMid meet" aria-hidden="true">{_rd_near()}</svg>')
    label = ('A road along the flank of a hill at night, eight stations on it, each lit by its own fire: the mine mouth with a laden ore tub '
             'on its rails; a brass balance on a stone pillar; an assayer’s table against the bedrock with a touchstone and needles by a copper '
             'brazier; a slate with the plan chalked on it under a lean-to; a helve hammer driven by a water wheel over an anvil with a hot bar; '
             'a stone quench trough with tongs holding a bar into the water; a grindstone over its trough; and the record house with a stamping '
             'block and a rack of finished work. The camera travels down the road to the station being read, which lights up and works.')
    html = (f'<div class="fig how road" role="img" aria-label="{label}"><div class="frame">{layers}</div>'
            f'<span class="tag ftag" id="st-tag">I · Capture</span></div>')
    return html, []

def how_css():
    """Each layer's position for each station (the camera), the machines' pivots: from the same numbers as the drawing."""
    out = [f'.how .pan.{k}{{aspect-ratio:{w}/{RD_VH}}}' for k, (x0, w, p) in RD_VB.items()]
    x0, w, p = RD_VB['mid']; out.append(f':root:not(.js) .how .pan.mid{{transform:translateX({-(RD_X[0] - 200 - x0) / w * 100:.3f}%)}}')   # without script the road scrolls from the mine
    for i, x in enumerate(RD_X):
        for k, (x0, w, p) in RD_VB.items():
            out.append(f'.how[data-st="{i}"] .pan.{k}{{transform:translateX({-(x * p - x0) / w * 100:.3f}%)}}')
    return ''.join(out) + ''.join(HOW_PIV)

def _chunks(rng, cx, cy, n, spread, rmin, rmax):
    """Ore: dark rock with metal in it, each chunk its own shape, its top catching the sky, flecks of metal in its faces."""
    rock, rims, flecks = [], [], []
    for _ in range(n):
        x, y, r = cx + rng.uniform(-spread, spread), cy + rng.uniform(-spread * .25, spread * .2), rng.uniform(rmin, rmax)
        P = [(x + r * math.cos(t) * rng.uniform(.7, 1.1), y + r * .8 * math.sin(t) * rng.uniform(.7, 1.1)) for t in (k * math.tau / 6 + rng.uniform(-.3, .3) for k in range(6))]
        rock.append(f'<path d="{poly_d(P)}" fill="{rng.choice(["#2E2A33", "#3A3440", "#24222A", "#433B46"])}"/>')
        rims.append(f'M{f(P[3][0])} {f(P[3][1])} L{f(P[4][0])} {f(P[4][1])} L{f(P[5][0])} {f(P[5][1])}')
        for _ in range(2): flecks.append((x + rng.uniform(-r * .5, r * .5), y + rng.uniform(-r * .4, r * .3)))
    return ''.join(rock) + f'<path d="{"".join(rims)}" fill="none" stroke="#9AA6C8" stroke-opacity=".35" stroke-width="1"/>', flecks

def _spray(x, y, n, seed, a0=-170, a1=-10, r0=10, r1=34):
    """A burst of sparks as short tapered streaks, drawn once; the burst animates the group, not each spark."""
    rng = random.Random(seed); s = []
    for _ in range(n):
        a = math.radians(rng.uniform(a0, a1)); r = rng.uniform(r0, r1); L = rng.uniform(4, 9)
        x1, y1 = x + math.cos(a) * r, y + math.sin(a) * r; x0, y0 = x + math.cos(a) * (r - L), y + math.sin(a) * (r - L)
        s.append(f'<path d="M{f(x0)} {f(y0)} L{f(x1)} {f(y1)}" stroke="{rng.choice(["#FFF4D8", "#FFE7A8", "#FFC24D", "#FF9440"])}" stroke-width="{rng.uniform(1, 1.8):.1f}" stroke-linecap="round"/>')
    return ''.join(s)

# ------------------------------------------------------------------------------------------------ Smith: the anvil
SMITH_O = (420, 0); SMITH_W, SMITH_H = 640, 700        # a closer frame on the hero's own forge
# the 2.6 s beat: (percent, (x, y, deg) in scene units, easing into the next key)
SMITH_KEYS = [(0, (731.7, 324.4, -12), 'cubic-bezier(.2,.8,.3,1)'), (5, (733, 306, -16), 'cubic-bezier(.3,0,.3,1)'), (12, (731.7, 321, -12.5), 'cubic-bezier(.45,0,.4,1)'),
              (32, (800, 196, 12), 'ease-in-out'), (54, hero.POSE_RAISED, 'ease-in-out'), (84, (hero.POSE_RAISED[0] + 14, hero.POSE_RAISED[1] - 34, hero.POSE_RAISED[2] + 8), 'cubic-bezier(.6,0,1,.6)'), (100, (731.7, 324.4, -12), '')]
def smith_css():
    x, y, w, h = HAMMER_BOX
    tf = lambda p: f'translate({p[0] / w * 100:.3f}%,{p[1] / h * 100:.3f}%) rotate({p[2]}deg)'
    keys = ''.join(f'{pc}%{{transform:{tf(p)}' + (f';animation-timing-function:{e}' if e else '') + '}' for pc, p, e in SMITH_KEYS)
    return f'@keyframes blow{{{keys}}}\n@media (prefers-reduced-motion: reduce){{.smith .shm{{transform:{tf(hero.POSE_RAISED)}!important}}}}'

def hammer_ov(o, vw, vh, cls):
    """The hammer as its own layer; its local origin sits at scene (0, 0); keyframes move it in scene units."""
    x, y, w, h = HAMMER_BOX
    st = (f'left:{(x - o[0]) / vw * 100:.3f}%;top:{(y - o[1]) / vh * 100:.3f}%;width:{w / vw * 100:.3f}%;height:{h / vh * 100:.3f}%;'
          f'transform-origin:{-x / w * 100:.3f}% {-y / h * 100:.3f}%;transform:translate({hero.POSE_RAISED[0] / w * 100:.3f}%,{hero.POSE_RAISED[1] / h * 100:.3f}%) rotate({hero.POSE_RAISED[2]}deg)')
    return f'<svg class="ov hm {cls}" viewBox="{x} {y} {w} {h}" style="{st}" aria-hidden="true">{hammer()}</svg>'

def smith():
    W, H = SMITH_W, SMITH_H; ox, oy = SMITH_O; T = f'translate({-ox} {-oy})'
    a = [f'<g transform="{T}">',
         '<ellipse cx="660" cy="300" rx="380" ry="260" fill="url(#halo)" opacity=".75"/>',
         '<use href="#hz-hearth"/>',
         f'<g class="sfl">{fire(hero.BY, hero.PLUMES, t=.35, sheet=(372, 1040, 52), cls=True, red=.42)}</g>',
         '<use href="#hz-front"/><use href="#hz-anvil"/><use href="#hz-tongs"/>']
    X0, Y0 = hero.sc(112, 80); X1, Y1 = hero.sc(150, 87)
    body = f'M{f(X0)} {f(Y0 + 4)} L{f(X0 + 3)} {f(Y0)} L{f(X1 - 3)} {f(Y0)} L{f(X1)} {f(Y0 + 4)} L{f(X1)} {f(Y1)} L{f(X0)} {f(Y1)} Z'
    a.append(f'<path d="{body}" fill="#5A1C10"/><path d="M{f(X0 + 3)} {f(Y0)} H{f(X1 - 3)} L{f(X1)} {f(Y0 + 4)} H{f(X0)} Z" fill="#A8401C"/>')
    a.append('</g>')
    sx, sy = hero.STRIKE[0] - ox, hero.STRIKE[1] - oy
    hot = (f'<g transform="{T}"><path d="M{f(X0 - 4)} {f(Y0 - 3)} H{f(X1 + 4)} V{f(Y1 + 2)} H{f(X0 - 4)} Z" fill="#FF8A2A" filter="url(#gl6)" opacity=".7"/>'
           f'<path d="{body}" fill="url(#billetH)"/><path d="M{f(X0 + 3)} {f(Y0)} H{f(X1 - 3)} L{f(X1)} {f(Y0 + 4)} H{f(X0)} Z" fill="url(#billetHTop)"/></g>')
    ovs = [ov(W, H, (X0 - ox - 10, Y0 - oy - 10, X1 - X0 + 20, Y1 - Y0 + 20), hot, 'sbar'),
           hammer_ov(SMITH_O, W, H, 'shm'), flash_ov(W, H, sx, sy, 110, 'sfx'), sparks_ov(W, H, sx, sy, 30, 4, 'ssp', 1.25)]
    return fig(W, H, ''.join(a), ''.join(ovs), 'smith fade-rb',
               'At the anvil, closer: the hearth burns behind and above; the hammer comes down on the glowing billet held in the tongs, the struck metal flashes white and sparks fly.')

# ------------------------------------------------------------------------------------------------ Rune: the quench, and the tower on the ridge
def _crescent(x, y, r, phase=.55):
    """A lit crescent: the outer limb a half circle, the terminator an ellipse arc (phase 0 thin .. 1 half)."""
    return f'M{f(x)} {f(y - r)} A{f(r)} {f(r)} 0 1 1 {f(x)} {f(y + r)} A{f(r * phase)} {f(r)} 0 1 0 {f(x)} {f(y - r)} Z'

def _far_tower(tx, base):
    """The Rune tower far off on the ridge, as the tower page draws it: a foundation with its lit door, four courses of
    ashlar narrowing as they rise, each with its rune tiles and its generation's band (blue, violet, gold, and gen 4 in
    orange heat), a crenellated crown with its fire. Seen through the night air: low contrast, a cool haze over it."""
    rng = random.Random(5); s = []
    courses = [(40, 46, 44, None), (36, 43, 40, '#5A8CFF'), (36, 40, 37, '#A67CFF'), (34, 37, 34, '#E2B04A'), (32, 34, 31, '#FF7A2A')]   # (height, hw below, hw above, band below)
    y = base
    for k, (h, hw0, hw1, band) in enumerate(courses):
        y0 = y - h
        P = [(tx - hw0, y), (tx + hw0, y), (tx + hw1, y0), (tx - hw1, y0)]
        s.append(f'<path d="{poly_d(P)}" fill="url(#towerStone)"/>')
        rows = 3 if k == 0 else 2; rh = h / rows
        for r in range(rows):                                                                     # ashlar joints
            yy = y - r * rh; hw = lerp(hw0, hw1, r / rows); n = 4 + (r + k) % 2
            s.append(f'<path d="M{f(tx - hw)} {f(yy)} H{f(tx + hw)}' + ''.join(f'M{f(tx - hw + 2 * hw * (q + rng.uniform(.35, .65)) / n)} {f(yy)} V{f(yy - rh)}' for q in range(n - 1)) + '" stroke="#0C0A12" stroke-width="1"/>')
        if k:                                                                                     # the course's rune tiles, two rows of four
            tw, th = 6.4, 6.6; gx = tx - 2 * (tw + 2.2) + 1.1
            hot = k == 4
            tiles = ''.join(f'M{f(gx + q * (tw + 2.2))} {f(y0 + 8 + r * (th + 2.4))} h{tw} v{th} h-{tw} Z' for r in range(2) for q in range(4))
            s.append(f'<path d="{tiles}" fill="{"#7A2A14" if hot else "#1A1722"}" stroke="{"#FF8A3A" if hot else "#4A4458"}" stroke-width=".8" stroke-opacity="{.9 if hot else .7}"/>')
            if hot: s.append(f'<path d="{poly_d(P)}" fill="none" stroke="#FF8A3A" stroke-opacity=".7" stroke-width="1.2"/>')
        if band: s.append(f'<path d="M{f(tx - hw0 - 1.5)} {f(y - 1.6)} H{f(tx + hw0 + 1.5)} V{f(y + 1.6)} H{f(tx - hw0 - 1.5)} Z" fill="{band}" opacity=".85"/>')
        s.append(f'<path d="M{f(tx - hw0)} {f(y)} L{f(tx - hw1)} {f(y0)}" stroke="#FFB070" stroke-opacity=".3" stroke-width="1.2"/>'      # the crown fire on the near-left edge
                 f'<path d="M{f(tx + hw0)} {f(y)} L{f(tx + hw1)} {f(y0)}" stroke="#8FA6D6" stroke-opacity=".35" stroke-width="1.2"/>')  # the sky on the right
        y = y0
    s.append(f'<path d="M{f(tx - 9)} {f(base)} V{f(base - 18)} L{f(tx)} {f(base - 25)} L{f(tx + 9)} {f(base - 18)} V{f(base)} Z" fill="url(#doorLit)" opacity=".85"/>')   # the lit door
    # the crown: an overhanging course, three merlons, the fire between them
    cy = y; s.append(f'<path d="M{f(tx - 37)} {f(cy)} H{f(tx + 37)} L{f(tx + 35)} {f(cy - 12)} H{f(tx - 35)} Z" fill="url(#towerStone)"/>'
                     f'<path d="M{f(tx - 37)} {f(cy)} H{f(tx + 37)}" stroke="#0C0A12" stroke-width="1.2"/>')
    for q in (-1, 0, 1):
        mx = tx + q * 24; s.append(f'<path d="M{f(mx - 7)} {f(cy - 12)} V{f(cy - 21)} H{f(mx + 7)} V{f(cy - 12)} Z" fill="url(#towerStone)"/><path d="M{f(mx - 7)} {f(cy - 21)} H{f(mx + 7)}" stroke="#FFB070" stroke-opacity=".55"/>')
    s.append(f'<ellipse cx="{tx}" cy="{f(cy - 30)}" rx="64" ry="46" fill="url(#halo)" opacity=".9"/>')
    for x, w, h, ph in ((tx, 11, 40, 0), (tx - 12, 7, 24, 1.4), (tx + 12, 7, 27, 2.6)):
        s.append(f'<path d="{tongue(x, cy - 12, w * 1.3, h * 1.1, .2, ph + .5, .7)}" fill="url(#fRed)" opacity=".7"/><path d="{tongue(x, cy - 12, w, h, .3, ph, .9)}" fill="url(#fOut)"/>'
                 f'<path d="{tongue(x, cy - 12, w * .5, h * .5, .4, ph + .6, .5)}" fill="url(#fMid)"/>')
    top = cy - 21
    hz = uid('th'); outline = f'M{f(tx - 46)} {f(base)} L{f(tx - 31)} {f(cy)} L{f(tx - 37)} {f(cy)} L{f(tx - 35)} {f(top)} H{f(tx + 35)} L{f(tx + 37)} {f(cy)} L{f(tx + 31)} {f(cy)} L{f(tx + 46)} {f(base)} Z'
    s.append(f'<clipPath id="{hz}"><path d="{outline}"/></clipPath>'                                   # the night air between us and it, on the tower only
             f'<path d="M{f(tx - 50)} {f(top - 10)} H{f(tx + 50)} V{f(base)} H{f(tx - 50)} Z" fill="url(#towerHaze)" clip-path="url(#{hz})"/>')
    return f'<g class="ftower" transform="translate({f(tx)} {f(base)}) scale(.76) translate({f(-tx)} {f(-base)})">{"".join(s)}</g>', top

def rune():
    W, H = 600, 600; s = []; rng = random.Random(21)
    # the brazier's light, ending inside the picture
    s.append('<ellipse cx="128" cy="300" rx="150" ry="170" fill="url(#haloG)" opacity=".55"/>')
    # the moons kindling over the far tower, as the tower page's sky: violet high, then gold, then the red of the newest
    arc = []
    for k in range(13):
        t = k / 12; x = lerp(300, 590, t); yb = 150 - 70 * math.sin(math.pi * t)
        band = k % 3
        y = yb + band * 22; r = (7.4, 6.2, 5.2)[band]; c = ('#C8B0FF', '#F0C860', '#FF7A4A')[band]
        arc.append(f'<path d="{_crescent(x, y, r, .25 + .5 * ((k * 7) % 5) / 4)}" fill="{c}" opacity="{(.75, .6, .5)[band]}"/>')
    s.append(f'<g class="fmoons">{"".join(arc)}</g>')
    # the ridge: a dark silhouette band with its crest lit, warm near the tower, cool elsewhere; fog at its foot
    rr = random.Random(23); crest = []
    for x in range(-60, 612, 12):              # from behind the tub, so the ridges have no edge of their own in the picture
        crest.append((x, 418 - 26 * math.exp(-((x - 452) / 90) ** 2) + rr.uniform(-4, 4) + (0 if x < 520 else (x - 520) * .12)))
    d = 'M-60 600 L' + ' L'.join(f'{f(x)} {f(y)}' for x, y in crest) + ' L612 600 Z'
    tower, ttop = _far_tower(452, 400)
    s.append(tower)
    s.append(f'<path d="{d}" fill="var(--ridge-b)"/>'
             f'<path d="M{" L".join(f"{f(x)} {f(y)}" for x, y in crest)}" fill="none" stroke="url(#ridgeEdge)" stroke-width="1.6"/>'
             f'<path d="M-60 404 H612 V480 H-60 Z" fill="url(#fogBand)" opacity=".55"/>')
    # a nearer ridge closes the picture's foot, with a few rocks on it
    near = [(-60, 600)] + [(x, 488 - 22 * math.sin((x - 232) / 70) + rr.uniform(-5, 5) - (x - 232) * .05) for x in range(-60, 612, 14)] + [(612, 600)]
    s.append(f'<path d="{poly_d(near)}" fill="var(--hill-b)"/><path d="M{" L".join(f"{f(x)} {f(y)}" for x, y in near[1:-1])}" fill="none" stroke="#8FA6D6" stroke-opacity=".3" stroke-width="1.4"/>'
             + ''.join(f'<path d="{poly_d([(x, y), (x + w * .3, y - h), (x + w * .7, y - h * .8), (x + w, y)])}" fill="var(--hill-rock)"/>' for x, y, w, h in ((300, 520, 40, 14), (420, 540, 56, 18), (530, 512, 36, 12))))
    # the ground behind the tub: two flagstones, lit green from the brazier
    for P in ([(0, 352), (120, 350), (126, 392), (0, 394)], [(124, 350), (232, 348), (236, 390), (128, 392)]):
        s.append(stone([(P, 'url(#sB1)', 0)], int(P[0][0]) + 3, u=1.8, pits=6, cracks=0, chisel=2, rims=[([P[0], P[1]], '#7CF0C4', .3, 1.2)]))
    # the brazier: an iron bowl with a riveted rim on three forged legs, its shadow on the flags
    s.append('<path d="M40 362 L190 358 L176 384 L52 386 Z" fill="url(#shadG)" opacity=".9"/>')
    s.append('<path d="M66 268 L56 356 L46 362 L60 362 L70 300 Z M108 270 L110 360 L102 366 L118 366 L118 270 Z M156 268 L166 354 L178 360 L164 362 L150 300 Z" fill="#16171C"/>'
             '<path d="M66 268 L56 356 M156 268 L166 354" stroke="#7CF0C4" stroke-opacity=".3"/>')
    bowl = [(36, 236), (184, 236), (176, 250), (156, 276), (64, 276), (44, 250)]
    s.append(f'<path d="{poly_d(bowl)}" fill="url(#band)"/><path d="M44 250 L64 276 L110 276 L110 250 Z" fill="#7CF0C4" opacity=".08"/><path d="M110 250 L156 276 L176 250 Z" fill="#000" opacity=".25"/>')
    s.append('<path d="M34 230 L186 230 L186 240 L34 240 Z" fill="#2A2C33"/><path d="M34 230 H186" stroke="#A8F5D2" stroke-opacity=".6" stroke-width="1.4"/>')
    s.append('<path d="' + ''.join(circ(x, 235, 1.8) for x in range(42, 182, 14)) + '" fill="url(#rivet)"/>')
    s.append(coals(rng, 14, 50, 170, 230, 4, 8, 12, .3, hotx=110).replace('#FFF4D8', '#C8FFE8').replace('#FFC24D', '#7CF0C4'))
    for x, w, h, ph in ((110, 22, 120, 0), (80, 14, 70, 1.4), (142, 15, 80, 2.6), (96, 9, 44, 4), (128, 9, 50, 5)):
        s.append(f'<g class="bz"><path d="{tongue(x, 230, w * 1.3, h * 1.1, .2, ph + .5, .7)}" fill="#1FA87A" opacity=".45"/><path d="{tongue(x, 230, w, h, .3, ph, .9)}" fill="url(#fCopper)"/>'
                 f'<path d="{tongue(x, 230, w * .45, h * .45, .4, ph + .6, .5)}" fill="url(#fCore)"/></g>')
    # the quench tub's back rim and inner wall
    TB = [(14, 392), (210, 386), (204, 470), (20, 474)]
    s.append(stone([(TB, 'url(#sB3)', 0)], 7, u=2, pits=10, cracks=1, chisel=4, rims=[([TB[0], TB[1]], '#A8F5D2', .45, 1.4)]))

    # ---- the quench, one layer in depth order: the water, the bar's drowned half seen through it, the water over that,
    # the green fire's reflection, the bar's dry half going in, the meniscus and ripples where it enters, the tub's front
    WATER = [(28, 398), (196, 392), (194, 424), (30, 430)]
    T = 'translate(10 40) rotate(20 58 336)'                                   # the bar, tilted down into the water
    ex, ey = 154.8, 406.5                                                      # where it enters
    cw, ca, cb = uid('qw'), uid('qa'), uid('qb')
    rng4 = random.Random(44)
    scale_ = ''.join(poly_d([(x, y), (x + rng4.uniform(2, 5), y + rng4.uniform(-1, 1)), (x + rng4.uniform(1, 4), y + rng4.uniform(1.2, 2.6))]) for x, y in
                     [(rng4.uniform(60, 140), rng4.uniform(330, 341)) for _ in range(12)] + [(rng4.uniform(66, 140), rng4.uniform(322, 326)) for _ in range(6)])
    bar = ('<path d="M-44 246 L36 312 L42 308 L-38 242 Z M-44 262 L38 326 L42 321 L-40 257 Z" fill="#26282E"/><path d="M-38 242 L42 308 M-40 257 L42 321" stroke="#A8B4C8" stroke-opacity=".4"/>'
           '<path d="M34 312 L46 316 L66 321 L66 326 L48 326 L34 318 Z M36 324 L50 346 L66 348 L66 344 L52 341 L42 324 Z" fill="#2E3038"/>'
           '<path d="M46 316 L66 321 M50 346 L66 348" stroke="#A8F5D2" stroke-opacity=".35"/><path d="M36 315 l5 -3 l5 3 l0 6 l-5 3 l-5 -3 Z" fill="#4A4C54"/>'
           '<path d="M58 330 L168 326.5 L174 322 L64 325.5 Z" fill="url(#billetTopQ)"/>'
           '<path d="M58 330 L168 326.5 L186 331 L168 340.5 L58 344 Z" fill="url(#billetQ)"/>'
           '<path d="M168 326.5 L174 322 L189 327.5 L186 331 Z" fill="#E86A2A" opacity=".85"/>'
           '<path d="M86 329.6 L88 343 M118 328.6 L120 342" stroke="#000" stroke-opacity=".3" stroke-width="1.4"/>'
           '<path d="M58 330 L168 326.5" stroke="#F2C8A8" stroke-opacity=".3"/>'
           f'<path d="{scale_}" fill="#141012" opacity=".55"/>'
           '<g class="q4h"><path d="M96 316 H160 V354 H96 Z" fill="url(#hotGlow)"/><path d="M100 328.4 L150 327 L150 341.6 L100 342.6 Z" fill="url(#billetHotQ)"/></g>')
    defs = (f'<defs><clipPath id="{cw}"><path d="{poly_d(WATER)}"/></clipPath>'
            f'<clipPath id="{ca}"><path d="M-200 0 H150 V700 H-200 Z"/></clipPath><clipPath id="{cb}"><path d="M150 0 H400 V700 H150 Z"/></clipPath></defs>')
    water = (f'<path d="{poly_d(WATER)}" fill="url(#waterM)"/><path d="M28 398 L196 392 L196 397 L28 403 Z" class="wsheen"/>')
    drowned = (f'<g clip-path="url(#{cw})"><g class="qm"><g transform="rotate(-14 {ex} {ey})" opacity=".6"><g transform="{T}" clip-path="url(#{cb})">'
               f'<path d="M58 330 L168 326.5 L186 331 L168 340.5 L58 344 Z" fill="#3A1A18"/><path d="M58 330 L168 326.5 L174 322 L64 325.5 Z" fill="#5A2A20"/>'
               f'<path d="M150 328 L168 326.5 L186 331 L168 340.5 L150 341 Z" fill="#B8401C" opacity=".6"/></g></g></g>'
               f'<path d="{poly_d(WATER)}" fill="#0C1A24" opacity=".42"/>'
               + '<g transform="translate(0 398) scale(1 -.2) translate(0 -398)" opacity=".42">'                          # the brazier's green fire, upside down in the water
               + ''.join(f'<path d="{tongue(x, 398, w, h, .3, ph, .9)}" fill="url(#fCopper)"/>' for x, w, h, ph in ((110, 22, 120, 0), (80, 14, 70, 1.4), (142, 15, 80, 2.6)))
               + '</g></g>')
    dry = f'<g class="qm"><g transform="{T}" clip-path="url(#{ca})">{bar}</g></g>'
    rings = ''.join(f'<ellipse class="rp" cx="{ex}" cy="{ey + .6}" rx="{r_}" ry="{f(r_ * .2)}" fill="none" stroke="#C8F5E2" stroke-width="{1.2 - i * .3:.1f}"/>' for i, r_ in enumerate((9, 18, 30)))
    meniscus = (f'<ellipse cx="{ex}" cy="{ey + .4}" rx="8" ry="2.2" fill="none" stroke="#E6FFF4" stroke-opacity=".75" stroke-width="1.2"/>'
                f'<ellipse cx="{ex}" cy="{ey + .4}" rx="16" ry="4" fill="url(#hotGlow)" opacity=".35"/><g class="rip">{rings}</g>'
                '<path d="M28 398 L196 392" stroke="#A8F5D2" stroke-opacity=".5"/>')
    front = [(10, 430), (214, 424), (216, 598), (8, 598)]
    tubf = (stone([(front, 'url(#sB2)', 84)], 9, u=2, pits=28, cracks=2, chisel=10, rims=[([front[0], front[1]], '#7CF0C4', .6, 1.8), ([front[0], front[3]], '#A8F5D2', .25, 1.2)])
            + rune_cut('laguz', 92, 476, 1.3, 'green', groove=7, core=2.2, glow=.5))
    steam = ov(W, H, (ex - 80, 150, 160, 270), f'<g class="qs">{wisp(ex, ey, 80, 220, 71, "vaporG", .9)}{wisp(ex + 14, ey - 4, 60, 170, 72, "vaporG", .7)}</g>', 'stm')
    ovs = [ov(W, H, (-60, 220, 300, 380), defs + water + drowned + dry + meniscus + tubf, 'quench'), steam, flash_ov(W, H, ex, ey, 90, 'rfx', 'haloG')]
    return fig(W, H, ''.join(s), ''.join(ovs), 'rune fade-x',
               'The quench at night: gen 4, still cherry-hot, goes into a stone tub of water at an angle in its tongs, lit green by an iron brazier; '
               'its drowned half shows dark through the water, ripples ring out from where it enters and steam bursts up. '
               'Far behind, on a dark ridge, the Rune tower stands in the night haze, its courses banded blue, violet, gold and orange, its crown fire lit, the first moons kindling above it.')

# ------------------------------------------------------------------------------------------------ Ward: nine rivets
def lick(x, y, w, h, ph):
    return (f'<path d="{tongue(x, y, w, h, .3, ph, .9)}" fill="url(#fBlue)"/>'
            f'<path d="{tongue(x, y, w * .45, h * .55, .4, ph + 1, .6)}" fill="#EAF2FF" opacity=".85"/>')

def ward():
    W, H = 600, 480; k = 2.6; tx, ty = 300 - 128 * k, 236 - 158 * k
    Mx = f'matrix({k} 0 0 {k} {f(tx)} {f(ty)})'
    s = ['<ellipse cx="300" cy="300" rx="300" ry="200" fill="url(#haloB)" opacity=".35"/>', f'<g transform="{Mx}">',
         '<ellipse cx="128" cy="236" rx="102" ry="6" fill="#000" opacity=".55" filter="url(#soft)"/>']
    STL = [(46, 150), (92, 148), (86, 232), (40, 226), (34, 190)]; STM = [(92, 148), (170, 146), (176, 234), (86, 232)]; STR = [(170, 146), (206, 150), (220, 186), (212, 228), (176, 234)]
    s.append(stone([(STL, 'url(#sL)', 76), (STM, 'url(#sM)', 88), (STR, 'url(#sR)', 100)], 91, u=.5, pits=110, cracks=5, chisel=70,
                   avoid=[(50, 168, 82, 210), (96, 168, 120, 210), (124, 168, 150, 210), (146, 168, 174, 210), (180, 172, 204, 210)],
                   rims=[([(46, 150), (34, 190), (40, 226)], '#8FA6D6', .45, 1.2), ([(206, 150), (220, 186), (212, 228)], '#8FA6D6', .45, 1.1), ([(46, 150), (92, 148), (170, 146), (206, 150)], '#C9D8F0', .45, .8)]))
    s.append('<path d="M46 150 L92 148 L170 146 L206 150 L203 153.5 L170 150 L92 152 L48 154 Z" fill="url(#bevel)" opacity=".6"/>')
    riv = along([(37, 162.6), (94, 159.6), (169, 157.6), (211, 161.6)], 9, .045, .955)
    s.append(band([(37, 160), (94, 157), (169, 155), (210, 159)], [(36, 170), (93, 167), (170, 165), (213, 169)], riv, u=.95, rr=1.3))
    for name, x, y, col in [('algiz', 57, 172, 'blue'), ('thurs', 100, 172, 'gold'), ('wunjo', 128, 172, 'green'), ('ingwaz', 150, 172, 'violet'), ('kaun', 184, 172, 'orange')]:
        s.append(rune_cut(name, x, y, 1, col))
    s.append(hero.seam_pool())
    s.append(anvil(seed=9, mark=False))
    s.append('</g>')
    ovs = []
    for i, (x, y) in enumerate(riv):
        X, Y = k * x + tx, k * y + ty; r = 30
        lk = ''.join(lick(X + dx, Y - 4, 3.2, 26 + 6 * (j % 2), i + j) for j, dx in enumerate((-13, 13)))
        ovs.append(ov(W, H, (X - r, Y - r - 20, 2 * r, 2 * r + 20), f'<circle class="hb" cx="{f(X)}" cy="{f(Y)}" r="{r}" fill="url(#haloB)"/><circle class="hg" cx="{f(X)}" cy="{f(Y)}" r="{r * .8:.0f}" fill="url(#halo)"/>'
                    f'<g class="lick" style="transform-origin:{f(X)}px {f(Y)}px">{lk}</g><circle class="core" cx="{f(X)}" cy="{f(Y)}" r="{3.1 * k:.1f}"/><circle cx="{f(X - 2.6)}" cy="{f(Y - 2.6)}" r="2.2" fill="#fff" opacity=".7"/>', 'wr', f'--i:{i}'))
    return fig(W, H, ''.join(s), ''.join(ovs), 'ward', "The standing stone and its anvil, the band round the stone in front: nine rivets, each set blue-hot in turn with blue flame licking up beside it, then cooling through gold to iron; the five runes glow below.")

# ------------------------------------------------------------------------------------------------ Moons: the chart
def chart_label(moons, total):
    """The chart's accessible name, from the same moons it draws: the phase rule below (a kill test a crescent, anything else a sliver)."""
    W_ = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
    w = lambda k: W_[k] if k < len(W_) else str(k)
    kill = [f'M-{n:02d}' for n, _, s in moons if s == 'kill']; thin = [s for _, _, s in moons if s != 'kill']; five = sum(wd == 5 for _, wd, _ in moons)
    parts = [f'the {w(len(moons))} wildest of {total} moonshots along the ecliptic, wildest first, each numbered by its id']
    if thin: parts.append(f'{w(len(thin))} thin {"sliver" if len(thin) == 1 else "slivers"}, ' + (('still an idea' if len(thin) == 1 else 'still ideas') if set(thin) == {'idea'} else ', '.join(f'{w(thin.count(t))} {t}' for t in sorted(set(thin)))))
    if kill:
        k = ' and '.join(kill) + (' a crescent at its kill test' if len(kill) == 1 else ' crescents at their kill tests')
        if thin: parts[-1] += ', and ' + k
        else: parts.append(k)
    if five: parts.append(f'the {w(five)} rated 5 of 5 drawn brighter')
    return 'A star chart over the forge: ' + '; '.join(parts) + '; Otava above them, its pointers on the pole; the forge glowing on the horizon.'

def moons_chart(moons, total):
    """moons: (number, wild, stage) for each moon drawn, wildest first; total: how many moonshots the export holds."""
    MOONS = len(moons)
    W, H = 600, 460; m = 24; s = []; rng = random.Random(5)
    # a few faint field stars, square and small
    s.append('<path d="' + ''.join(f'M{f(rng.uniform(m, W - m))} {f(rng.uniform(m, 330))}h1.2v1.2h-1.2z' for _ in range(60)) + '" fill="#DCE4FF" opacity=".45"/>')
    # Otava, true to the sky, its pointers on the pole
    P = hero.otava(150, 112, 7.2)
    s.append('<g class="ot">' + ''.join(f'<line x1="{f(P[a][0])}" y1="{f(P[a][1])}" x2="{f(P[b][0])}" y2="{f(P[b][1])}"/>' for a, b in hero.OTAVA_LINES) + '</g>')
    dx, dy = P[0][0] - P[1][0], P[0][1] - P[1][1]; L = math.hypot(dx, dy)
    s.append(f'<line class="ptr" x1="{f(P[0][0])}" y1="{f(P[0][1] - 6)}" x2="{f(P[0][0] + dx / L * (P[0][1] - m - 6))}" y2="{m}"/><text x="{f(P[0][0] + 8)}" y="{m + 10}" class="lbl">TO THE POLE</text>')
    for i, (x, y) in enumerate(P):
        s.append(f'<g class="st" style="--i:{i}">{star4(x, y, 6.5, "#F0D392")}<path d="M{f(x - 1.4)} {f(y - 1.4)}h2.8v2.8h-2.8z" fill="#FFF8E8"/></g>')
    s.append(f'<text x="{f(min(p[0] for p in P))}" y="{f(max(p[1] for p in P) + 24)}" class="lbl">OTAVA</text>')
    # the ecliptic: an arc over the forge; twenty moons on it, numbered below on leader ticks
    A, C, B = (m + 8, 318), (300, 196), (W - m - 8, 214)
    q = lambda t: ((1 - t) ** 2 * A[0] + 2 * t * (1 - t) * C[0] + t * t * B[0], (1 - t) ** 2 * A[1] + 2 * t * (1 - t) * C[1] + t * t * B[1])
    s.append(f'<path class="ecl" d="M{A[0]} {A[1]} Q{C[0]} {C[1]} {B[0]} {B[1]}"/>')
    s.append(f'<text x="{W - m}" y="{B[1] - 22}" text-anchor="end" class="lbl">ECLIPTIC</text>')
    for i, (n, wild, stg) in enumerate(moons):
        x, y = q((i + .5) / MOONS); r = 9
        ph = .34 if stg == 'kill' else .12        # the stage as the phase: an idea a thin sliver, a kill test a crescent (as in Rune's sky)
        def cr(p_): return f'M{f(x)} {f(y - r)} A{r} {r} 0 1 1 {f(x)} {f(y + r)} A{f(abs(r * (1 - p_ * 1.6)) if abs(p_ - .625) > .01 else r * .01)} {r} 0 1 {0 if p_ < .625 else 1} {f(x)} {f(y - r)} Z'
        cres = cr(ph)   # the terminator softened: the lit side drawn three times, each a little further into the dark at less light
        ll = 12 if i % 2 == 0 else 26
        g = [f'<g class="moon w{wild}{" kt" if stg == "kill" else ""}" style="--i:{i}"><circle class="disc" cx="{f(x)}" cy="{f(y)}" r="{r}"/><path class="lit" d="{cr(ph + .12)}" fill="url(#moonLit)" opacity=".18"/><path class="lit" d="{cr(ph + .06)}" fill="url(#moonLit)" opacity=".4"/><path class="lit" d="{cres}" fill="url(#moonLit)"/><circle class="ring" cx="{f(x)}" cy="{f(y)}" r="{r + 2.5}"/>']
        o = ' o' if i % 2 else ''   # every other number, dropped on phones so the rest can be read at 10 px or more
        g.append(f'<line class="lead{o}" x1="{f(x)}" y1="{f(y + r + 5)}" x2="{f(x)}" y2="{f(y + r + 5 + ll)}"/><text class="num{o}" x="{f(x)}" y="{f(y + r + 17 + ll)}" text-anchor="middle">{n:02d}</text></g>')
        s.append(''.join(g))
    # the horizon: the range, and the forge on it, its glow and a column of sparks going up
    s.append('<ellipse cx="440" cy="420" rx="170" ry="70" fill="url(#halo)"/>')
    ridge = [(-10, 460), (-10, 412), (60, 396), (110, 410), (170, 384), (240, 404), (300, 392), (360, 410), (520, 408), (560, 390), (610, 400), (610, 460)]
    s.append(f'<path d="{poly_d(ridge)}" fill="url(#ridgeLit)"/>')
    # each peak split into a lit face toward the forge and a face in shadow away from it
    peaks = [(60, 396, 110, 410), (170, 384, 240, 404), (300, 392, 360, 410), (560, 390, 520, 408)]
    s.append('<path d="' + ''.join(poly_d([(px, py), (vx, vy), (vx + (8 if vx > px else -8), 460), (px, 460)]) for px, py, vx, vy in peaks if (vx > px) == (px < 440)) + '" fill="#000" opacity=".28"/>')
    s.append('<path d="M60 396 L110 410 M170 384 L240 404 M560 390 L610 400 M-10 412 L60 396 M110 410 L170 384" fill="none" stroke="#E09060" stroke-opacity=".45"/>')
    # the hero's forge seen from afar: the hearth courses, the stone with its band, the anvil against the glow
    k = .14; T_ = f'translate({f(440 - 700 * k)} {f(414 - 880 * k)}) scale({k})'
    s.append('<ellipse cx="440" cy="372" rx="120" ry="64" fill="url(#halo)" opacity=".9"/>')
    # the hearth is the ridge itself rising into a knoll, in the ridge's own light: no block of its own
    s.append('<path d="M352 414 C372 410 380 372 394 358 C404 350 470 348 484 354 C500 364 506 404 528 414 Z" fill="url(#ridgeLit)"/>'
             '<path d="M392 360 C398 353 404 351 412 350 C440 348 466 348 482 354" fill="none" stroke="#FFB070" stroke-opacity=".45" stroke-width="1.2"/>')
    s.append(f'<g transform="{T_}">'
             '<path d="M346 404 L430 372 L600 350 L760 356 L960 372 L1056 404 Z" fill="#E2481A"/><path d="M420 400 L600 362 L800 366 L980 392 Z" fill="#FFC24D" opacity=".8"/>'
             + ''.join(f'<path d="{fire_mini(x, 380)}" transform="translate({x} 380) scale({sc_}) translate({-x} -380)" fill="url(#fOut)"/>' for x, sc_ in ((640, 9), (548, 6), (852, 5.5)))
             + f'<g transform="{hero_M()}"><path d="{STONE_D}" class="hz2"/><path d="M37 160 L210 159 L213 169 L36 170 Z" fill="#000" opacity=".4"/>'
             f'<path d="{ANVIL_D}" class="hz2"/><path d="M72 86 H164 C176 86 196 90 222 96" fill="none" stroke="#FFB070" stroke-width="2.4"/>'
             '<path d="M66 147.5 H190" stroke="#FF8A2A" stroke-width="3"/><path d="M46 150 L92 148 L170 146 L206 150" fill="none" stroke="#FFB070" stroke-width="2" stroke-opacity=".8"/></g>'
             '<path d="M340 412 H1062" stroke="#FFB070" stroke-width="6" stroke-opacity=".8"/></g>')
    s.append('<path d="M300 392 L360 410 M520 408 L560 390" fill="none" stroke="#FFB070" stroke-opacity=".55" stroke-width="1.4"/>')
    s.append('<g class="sparks">' + ''.join(f'<path style="--i:{i}" d="M{f(444 + rng.uniform(-12, 12))} {f(rng.uniform(250, 360))}h1.4v1.4h-1.4z" fill="{rng.choice(["#FFC24D", "#FF9440", "#FFE9B0", "#45E0A0"])}"/>' for i in range(14)) + '</g>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{chart_label(moons, total)}">' + ''.join(s) + '</svg>')

STONE_D = 'M46 150 L92 148 L170 146 L206 150 L220 186 L212 228 L176 234 L86 232 L40 226 L34 190 Z'
def hero_M(): return hero.M

def fire_mini(x, y):
    return f'M{x - 7} {y} C{x - 8} {y - 10} {x - 2} {y - 14} {x} {y - 26} C{x + 3} {y - 14} {x + 9} {y - 10} {x + 7} {y} Z'

# ------------------------------------------------------------------------------------------------ Record: forged scales and the ledger
GAUGES = [('4.4', 'evidence runs per task: 7,390 runs over 1,662 tasks', 4.4, 10, ['0', '2', '4', '6', '8', '10'], None, '4.4, on a scale of 0 to 10'),
          ('42', 'proofs for every hundred tasks closed', 41.5, 100, ['0', '20', '40', '60', '80', '100 %'], None, '42, on a scale of 0 to 100 %'),
          ('3 min 14 s', 'the median full check, over 1,053 runs', 194, 600, ['0', '2', '4', '6', '8', '10 min'], None, '3 min 14 s, on a scale of 0 to 10 min'),
          ('58.5 ms', "the Ward's median check on a shell command; red is over its 150 ms budget", 58.5, 200, ['0', '40', '80', '120', '160', '200 ms'], 150, '58.5 ms, on a scale of 0 to 200 ms')]
def gauges():
    out = []
    for gi, (big, txt, v, mx, labels, hot, aria) in enumerate(GAUGES):
        w = 600 * v / mx; rng = random.Random(gi); s = [f'<svg viewBox="-8 0 616 76" role="img" aria-label="{aria}">']
        s.append('<path d="M0 34 H600 L604 38 V52 L600 56 H0 L-4 52 V38 Z" fill="url(#brassPlate)"/><path d="M0 34.6 H600" stroke="#FFF4D0" stroke-opacity=".8"/><path d="M0 55.4 H600" stroke="#3A2408" stroke-opacity=".6"/>')
        if hot: s.append(f'<path d="M{600 * hot / mx:.1f} 35 H600 V55 H{600 * hot / mx:.1f} Z" fill="#B12E16" opacity=".55"/>')
        tk = []
        for t in range(21):
            x = t * 30; mm = t % 4 == 0; tk.append(f'M{x} 35 V{46 if mm else 41}')
        s.append(f'<path d="{"".join(tk)}" stroke="#3A2408" stroke-width="1.2"/><path d="{"".join(tk).replace(" 35 V", " 35.8 V")}" stroke="#FFF4D0" stroke-opacity=".5" stroke-width=".6" transform="translate(.9 0)"/>')
        top = [(0, 20)] + [(x, 20 + rng.uniform(-.7, .7)) for x in [w * q for q in (.2, .4, .6, .8)]] + [(w, 20), (w, 31)]
        bar = poly_d(top + [(0, 31)])
        flakes = ''.join(poly_d([(x_, y_), (x_ + rng.uniform(1.4, 3), y_ - .6), (x_ + rng.uniform(2, 3.6), y_ + rng.uniform(.8, 1.6)), (x_ + .4, y_ + 1.2)])
                         for x_, y_ in ((rng.uniform(3, max(5, w * .5)), rng.uniform(22.5, 28.5)) for _ in range(int(w / 9) + 2)))
        s.append(f'<g class="fillg" style="--d:{gi * .15:.2f}s"><path d="{bar}" fill="#000" opacity=".45" transform="translate(1 3)"/>'
                 f'<path d="M{w - 26:.1f} 13 H{w + 12:.1f} V38 H{w - 26:.1f} Z" fill="url(#tipGlow)"/><path class="fill" d="{bar}"/>'
                 f'<path d="M0 21 H{w:.1f}" stroke="#FFF1C8" stroke-opacity=".55"/><path d="{flakes}" fill="#2A0C06" opacity=".55"/>'
                 f'<path d="M{max(0, w - 20):.1f} 20 H{w:.1f} V31 H{max(0, w - 20):.1f} Z" fill="url(#tipCore)"/><path d="M{w:.1f} 20 V31" stroke="#FFFBF0" stroke-width="1.2"/></g>')
        for j, lab in enumerate(labels):
            s.append(f'<text x="{j * 120}" y="72" text-anchor="middle">{lab}</text>')
        s.append('</svg>')
        out.append(f'<figure class="gauge"><div class="read"><b>{big}</b><span>{txt}</span></div>{"".join(s)}</figure>')
    return ''.join(out)

LEDGER = [('2 October', 13, 57), ('3 October', 96, 287), ('4 October', 57, 254), ('6 October', 35, 197), ('8 October', 4, 18), ('9 October', 158, 605),
          ('14 October', 45, 196), ('15 October', 113, 321), ('16 October', 211, 744), ('17 October', 0, 1), ('20 October', 37, 164), ('21 October', 62, 307),
          ('22 October', 115, 814), ('23 October', 156, 741), ('27 October', 3, 10), ('28 October', 67, 265), ('29 October', 127, 452), ('30 October', 99, 499),
          ('31 October', 169, 1033), ('1 November', 65, 304), ('2 November', 30, 121)]
def ledger(Wd=1200, cls='wide', step=3, vals=True):
    import datetime as dt
    days = {}
    for d, t, e in LEDGER:
        dd, mm = d.split(); days[(10 if mm == 'October' else 11, int(dd))] = (t, e)
    start = dt.date(2026, 10, 2); n = 32; bw = Wd / n; base = 262; sc = 230 / 211
    tid = f'ledger-t-{cls}'
    s = [f'<svg class="{cls}" viewBox="0 0 {Wd} 300" role="img" aria-labelledby="{tid}"><title id="{tid}">Tasks closed each day, 2 October to 2 November 2026: 21 working days, 1,662 tasks in all, the most on 16 October with 211.</title>']
    s.append(f'<line class="base" x1="0" x2="{Wd}" y1="{base + .5}" y2="{base + .5}"/>')
    for v in (50, 100, 150, 200):
        y = base - v * sc; s.append(f'<line class="tick" x1="0" x2="{Wd}" y1="{y:.1f}" y2="{y:.1f}" stroke-dasharray="2 6"/><text x="0" y="{y - 5:.1f}">{v}</text>')
    for i in range(n):
        d = start + dt.timedelta(days=i); key = (d.month, d.day); x = i * bw + bw * .22; w = bw * .56; cx = i * bw + bw / 2
        if key in days and days[key][0] > 0:
            t, e = days[key]; y = base - max(3, t * sc)
            lab = f'{d.day} {"Oct" if d.month == 10 else "Nov"}'
            ings, caps = [], []; yy = base
            for j in range(0, t, 10):
                ih = min(10, t - j) * sc; y1 = yy - ih + (.9 if ih > 3 else 0)
                ings.append(f'M{x:.1f} {yy:.1f} L{x + 1.4:.1f} {y1:.1f} H{x + w - 1.4:.1f} L{x + w:.1f} {yy:.1f} Z'); caps.append(f'M{x + 1.4:.1f} {y1:.1f} H{x + w - 1.4:.1f} V{y1 + 1.2:.1f} H{x + 1.4:.1f} Z'); yy -= ih
            pk = ' peak' if t == 211 else ''
            s.append(f'<g class="d" tabindex="0" style="--i:{i}"><title>{lab}: {t} tasks closed, {e} evidence runs</title>'
                     f'<path class="ing{pk}" d="{"".join(ings)}"/><path class="cap" d="{"".join(caps)}"/>' + (f'<text class="val" x="{cx:.1f}" y="{y - 8:.1f}" text-anchor="middle">{t} · {e} runs</text>' if vals else '') + '</g>')
        elif key in days:
            s.append(f'<g class="d" tabindex="0"><title>{d.day} Oct: 0 tasks closed, 1 evidence run</title><circle cx="{cx:.1f}" cy="{base}" r="2.2" class="none"/></g>')
        else:
            s.append(f'<circle cx="{cx:.1f}" cy="{base}" r="1.6" class="none"/>')
        if i % step == 0:
            s.append(f'<text x="{max(0, cx - bw / 2) if i == 0 else cx:.1f}" y="{base + 22}" text-anchor="{"start" if i == 0 else "middle"}">{d.day} {"Oct" if d.month == 10 else "Nov"}</text>')   # the first sits inside the frame
    s.append('</svg>')
    return ''.join(s)

# ------------------------------------------------------------------------------------------------ the footer: the fire banked for the night
def banked():
    """The forge at night, seen as in the hero: the hearth courses behind, its bed banked under ash with the cracks breathing,
    two low blue flames; the stone with its band and cold runes; the anvil, and the hammer laid on its face; embers rising."""
    W, H = 600, 400; rng = random.Random(14); s = []; k = 2.4; tx, ty = 300 - 128 * k, 150 - 86 * k
    s.append('<ellipse cx="300" cy="150" rx="330" ry="150" fill="url(#halo)" opacity=".4"/><path d="M-20 150 H620 V440 H-20 Z" class="jt"/>')
    for i, (ya, yb) in enumerate(((150, 214), (214, 282), (282, 360), (360, 440))):
        x = -20 + (i % 2) * 46
        while x < 620:
            w_ = rng.uniform(90, 150); c1 = rng.uniform(3, 7)
            P = [(x + 3 + c1, ya + 2), (x + w_ - 3, ya + 2), (x + w_ - 3, yb - 2), (x + 3, yb - 2), (x + 3, ya + 2 + c1)]
            s.append(stone([(P, 'url(#sB4)', 80)], rng.randrange(1 << 30), u=2, pits=6, cracks=1, chisel=3, rims=[([P[0], P[1]], '#FF9A5A', .3 if i == 0 else .1, 1.2)]))
            x += w_
    s.append('<path d="M-20 150 H620 V440 H-20 Z" fill="url(#bankFall)"/>')
    s.append('<path d="M-10 152 L40 124 L160 112 L300 106 L440 112 L560 124 L610 152 Z" fill="#140B0A"/>')
    s.append(coals(rng, 52, 0, 600, 148, 26, 10, 18, .12, hotx=300, crack_cls='bkc').replace('#1A0F0E', 'url(#ashG)').replace('fill="#5A3020" opacity=".5"', 'fill="#8A848C" opacity=".35"'))
    s.append(f'<g class="bk">{lick(262, 126, 6, 26, 1)}{lick(338, 124, 5, 18, 3)}</g>')
    s.append(f'<g transform="matrix({k} 0 0 {k} {f(tx)} {f(ty)})">'
             + stone([(STONE_L, 'url(#sL)', 76), (STONE_M, 'url(#sM)', 88), (STONE_R, 'url(#sR)', 100)], 93, u=.6, pits=70, cracks=4, chisel=40,
                     avoid=[(50, 168, 82, 210), (96, 168, 120, 210), (124, 168, 150, 210), (146, 168, 174, 210), (180, 172, 204, 210)],
                     rims=[([(46, 150), (34, 190), (40, 226)], '#FFB070', .35, 1.2), ([(206, 150), (220, 186), (212, 228)], '#8FA6D6', .45, 1.1)])
             + band(BAND_T, BAND_B, RIVETS, u=.95)
             + ''.join(f'<g opacity=".45">{rune_cut(n, x, y, 1, c, glow=.2)}</g>' for n, x, y, c in FRONT_RUNES)
             + '<path d="M66 147.5 H190" stroke="#8A2A10" stroke-width="3" stroke-opacity=".8"/>'
             + anvil(seed=21, mark=False) + '</g>')
    s.append(f'<g transform="translate(196 102) rotate(-2) scale(.75)">{hammer(hand=False)}</g>')
    emb = ''.join(f'<path style="--i:{i};--x:{rng.uniform(-30, 30):.0f}px;--t:{rng.uniform(4, 7):.1f}s" d="M{f(rng.uniform(80, 520))} {f(rng.uniform(100, 140))} l1 2.4 l-1 1.6 l-1 -1.6 Z" fill="{rng.choice(["#FFC24D", "#FF9440", "#FF6A4A", "#6E9BFF"])}"/>' for i in range(14))
    return fig(W, H, ''.join(s), ov(W, H, (60, -60, 480, 200), f'<g class="emr">{emb}</g>', 'emb'), 'banked',
               'The forge at night: the hearth banked under ash, its coals glowing only in their cracks and breathing slowly, two small blue flames; in front, the stone with its band and cold runes, the anvil, and the hammer laid on its face; a few embers rising.')
