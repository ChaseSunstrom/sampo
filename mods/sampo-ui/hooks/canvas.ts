// T-3701 (rework): the pane's hero pictures, after the live page's Board (plugin/web/live/board.js): each one large
// drawing of a mechanism. A canvas is an RGB pixel buffer one pixel wide and two high per cell, written out as half
// blocks (▀: the top pixel the glyph's colour, the bottom one the ground), with text cells over it for words and crisp
// box lines. Pure: no $; every colour from the palette (C).
import { C, cells, memo, mix } from './kit'

type Pt = readonly [number, number]
type Bez = readonly [Pt, Pt, Pt, Pt]
type Cell = readonly [string, number, number | undefined]

const clamp = (x: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, x))

/** A narrow-only copy of s: no marks, controls or format characters, and a wide glyph (2 columns) as one ·, so each
 * character is one cell of the picture. */
export function narrow(s: string): string {
  let out = ''
  for (const ch of s) {
    if (/\p{M}|\p{Cf}|\p{Cc}|\p{Cs}/u.test(ch)) continue // a lone surrogate (Cs) too
    const c = ch.codePointAt(0)!
    const wide = (c >= 0x1100 && c <= 0x115f) || (c >= 0x2e80 && c <= 0xa4cf) || (c >= 0xac00 && c <= 0xd7a3) ||
      (c >= 0xf900 && c <= 0xfaff) || (c >= 0xfe30 && c <= 0xfe4f) || (c >= 0xff00 && c <= 0xff60) ||
      (c >= 0xffe0 && c <= 0xffe6) || c >= 0x1f000
    out += wide ? '·' : ch
  }
  return out
}

/** mulberry32 from a string: a picture's layout holds still from one draw to the next (board.js's seed). */
export function seed(s: string): () => number {
  let h = 1779033703 ^ s.length
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 3432918353), (h = (h << 13) | (h >>> 19))
  let a = h >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const bez = (a: number, b: number, c: number, d: number, t: number) => {
  const u = 1 - t
  return u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * d
}
/** An S-curve from a to b: leaves and arrives level (board.js's link). */
export const link = (x0: number, y0: number, x1: number, y1: number): Bez => {
  const m = (x0 + x1) / 2
  return [[x0, y0], [m, y0], [m, y1], [x1, y1]]
}
/** P moved dy pixels down: a hairline beside it */
const shift = (P: Bez, dy: number): Bez => [[P[0][0], P[0][1] + dy], [P[1][0], P[1][1] + dy], [P[2][0], P[2][1] + dy], [P[3][0], P[3][1] + dy]]
export const along = (P: Bez, u: number): Pt => [bez(P[0][0], P[1][0], P[2][0], P[3][0], u), bez(P[0][1], P[1][1], P[2][1], P[3][1], u)]

export type Canvas = ReturnType<typeof canvas>

type Still = { px: number[]; text: Map<number, Cell>; fc: Int32Array; fa: Float32Array }
// a braille cell's dot bits: [column][row] of its 2×4 dots
const BRAILLE = [[0x01, 0x02, 0x04, 0x40], [0x08, 0x10, 0x20, 0x80]] as const

/** A canvas of `cols`×`rows` cells: `cols` pixels across, 2×`rows` down, on the Board's ground; over the pixels a
 * braille layer twice as fine each way (2×4 dots a cell) for dots and hairlines, so clouds and curves never look
 * blocky; text over both. Coordinates are pixels throughout (fractions reach the finer dots). */
export function canvas(cols: number, rows: number, from?: Still) {
  const W = Math.max(1, cols), H = Math.max(1, rows) * 2, FW = W * 2, FH = H * 2
  const px = from ? from.px.slice() : new Array<number>(W * H).fill(C.bg)
  const text = from ? new Map(from.text) : new Map<number, Cell>()
  const fc = from ? from.fc.slice() : new Int32Array(FW * FH).fill(-1) // a fine dot's colour (-1: none) …
  const fa = from ? from.fa.slice() : new Float32Array(FW * FH) // … and its strength: the brightest dot of a cell colours it
  const fdot = (x: number, y: number, color: number, a = 1) => {
    const X = Math.floor(x * 2), Y = Math.floor(y * 2)
    if (X < 0 || Y < 0 || X >= FW || Y >= FH) return
    const i = Y * FW + X
    if (a >= fa[i]!) fc[i] = color, fa[i] = a
  }
  const at = (x: number, y: number) => {
    const X = Math.round(x), Y = Math.round(y)
    return X < 0 || Y < 0 || X >= W || Y >= H ? -1 : Y * W + X
  }
  const put = (i: number, color: number, a: number) => {
    if (i >= 0) px[i] = a >= 1 ? color : mix(px[i]!, color, a)
  }
  /** pixels along a path, each once (so a translucent stroke never darkens where its steps overlap); a wide one
   * centred on the path */
  const stroke = (pts: Pt[], color: number, a: number, width: number) => {
    const seen = new Set<number>(), off = Math.floor((width - 1) / 2)
    for (let k = 0; k < pts.length; k++) {
      const [x0, y0] = pts[k]!, [x1, y1] = pts[Math.min(pts.length - 1, k + 1)]!
      const n = Math.max(1, Math.ceil(Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0))))
      for (let s = 0; s <= n; s++)
        for (let w = 0; w < width; w++) {
          const i = at(x0 + ((x1 - x0) * s) / n, y0 + ((y1 - y0) * s) / n + w - off)
          if (i >= 0 && !seen.has(i)) seen.add(i), put(i, color, a)
        }
    }
  }
  const api = {
    cols: W, rows: H / 2,
    dot: (x: number, y: number, color: number, a = 1) => put(at(x, y), color, a),
    rect: (x: number, y: number, w: number, h: number, color: number, a = 1) => {
      for (let j = Math.round(y); j < Math.round(y + h); j++) for (let i = Math.round(x); i < Math.round(x + w); i++) put(at(i, j), color, a)
    },
    line: (x0: number, y0: number, x1: number, y1: number, color: number, a = 1, width = 1) => stroke([[x0, y0], [x1, y1]], color, a, width),
    /** a dashed line: `on` pixels drawn, then as many skipped */
    dashed: (x0: number, y0: number, x1: number, y1: number, color: number, a = 1, on = 1) => {
      const n = Math.max(1, Math.ceil(Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0))))
      for (let s = 0; s <= n; s++) if (s % (2 * on) < on) put(at(x0 + ((x1 - x0) * s) / n, y0 + ((y1 - y0) * s) / n), color, a)
    },
    curve: (P: Bez, color: number, a = 1, width = 1) => {
      const n = Math.max(8, Math.ceil(Math.hypot(P[3][0] - P[0][0], P[3][1] - P[0][1]) * 1.5))
      stroke(Array.from({ length: n + 1 }, (_, k) => along(P, k / n)), color, a, width)
    },
    /** a trapezoid from x0 (top t0, bottom b0) to x1 (t1, b1): a column of pixels per x, `fill` inside, its outline
     * (each column's ends, the two sides whole) in `edge`; crisp half blocks, no braille over the fill */
    trap: (x0: number, x1: number, t0: number, b0: number, t1: number, b1: number, fill: number, a: number, edge: number) => {
      const X0 = Math.round(x0), X1 = Math.round(x1)
      for (let x = X0; x <= X1; x++) {
        const u = X1 > X0 ? (x - X0) / (X1 - X0) : 0
        const t = Math.round(t0 + (t1 - t0) * u), b = Math.round(b0 + (b1 - b0) * u)
        for (let y = t; y <= b; y++) put(at(x, y), x === X0 || x === X1 || y === t || y === b ? edge : fill, x === X0 || x === X1 || y === t || y === b ? 1 : a)
      }
    },
    /** a soft light: alpha falling off with the square of the distance */
    glow: (x: number, y: number, r: number, color: number, a: number) => {
      for (let j = Math.floor(y - r); j <= Math.ceil(y + r); j++)
        for (let i = Math.floor(x - r); i <= Math.ceil(x + r); i++) {
          const d = Math.hypot(i - x, j - y) / r
          if (d < 1) put(at(i, j), color, a * (1 - d) * (1 - d))
        }
    },
    /** words as cells over the picture (cell column, cell row); `bg` left out: the pixels under them */
    write: (col: number, row: number, s: string, fg: number, bg?: number) => {
      let c = Math.round(col)
      const r = Math.round(row)
      if (r < 0 || r >= H / 2) return
      for (const ch of narrow(s)) {
        if (c >= 0 && c < W) text.set(r * W + c, [ch, fg, bg])
        c++
      }
    },
    /** a rounded box of cells (╭─╮ │ ╰─╯), its inside filled with `fill` when given */
    box: (col: number, row: number, w: number, h: number, color: number, fill?: number) => {
      if (w < 2 || h < 2) return
      if (fill !== undefined) api.rect(col + 1, (row + 1) * 2, w - 2, (h - 2) * 2, fill) // the inside in pixels: drawable over
      for (let j = 0; j < h; j++)
        for (let i = 0; i < w; i++) {
          if (!(j === 0 || j === h - 1 || i === 0 || i === w - 1)) continue
          const ch = j === 0 ? (i === 0 ? '╭' : i === w - 1 ? '╮' : '─') : j === h - 1 ? (i === 0 ? '╰' : i === w - 1 ? '╯' : '─') : '│'
          api.write(col + i, row + j, ch, color, fill)
        }
    },
    // the fine layer: a braille dot, a hairline, a curve (pixel coordinates, fractions welcome)
    fdot,
    fline: (x0: number, y0: number, x1: number, y1: number, color: number, a = 1) => {
      const n = Math.max(1, Math.ceil(Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0)) * 2))
      for (let s = 0; s <= n; s++) fdot(x0 + ((x1 - x0) * s) / n, y0 + ((y1 - y0) * s) / n, color, a)
    },
    fcurve: (P: Bez, color: number, a = 1) => {
      const n = Math.max(8, Math.ceil(Math.hypot(P[3][0] - P[0][0], P[3][1] - P[0][1]) * 3))
      for (let k = 0; k <= n; k++) fdot(...along(P, k / n), color, a)
    },
    /** a dashed hairline: `on` fine dots drawn, then as many skipped */
    fdashed: (x0: number, y0: number, x1: number, y1: number, color: number, a = 1, on = 2) => {
      const n = Math.max(1, Math.ceil(Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0)) * 2))
      for (let s = 0; s <= n; s++) if (s % (2 * on) < on) fdot(x0 + ((x1 - x0) * s) / n, y0 + ((y1 - y0) * s) / n, color, a)
    },
    /** a packet riding a hairline: a bright 2×2 of fine dots */
    packet: (x: number, y: number, color: number) => {
      for (const dx of [-0.25, 0.25]) for (const dy of [-0.25, 0.25]) fdot(x + dx, y + dy, mix(color, 0xffffff, 0.45), 1.01) // lit: brighter than its line, so a cell it shares is its colour
    },
    /** the still layer, so a live one can start from it */
    copy: (): Still => ({ px, text, fc, fa }),
    /** Raster cells: the word written there; else its braille dots, coloured by the brightest, over its pixels'
     * average; else its two pixels as ▀ */
    cells: () => {
      const out: (readonly [string, number, number])[] = []
      for (let r = 0; r < H / 2; r++)
        for (let c = 0; c < W; c++) {
          const top = px[2 * r * W + c]!, bot = px[(2 * r + 1) * W + c]!
          const t = text.get(r * W + c)
          if (t) {
            out.push([t[0], t[1], t[2] ?? C.bg])
            continue
          }
          let bits = 0, best = -1, color = 0
          for (let dx = 0; dx < 2; dx++)
            for (let dy = 0; dy < 4; dy++) {
              const i = (4 * r + dy) * FW + 2 * c + dx
              if (fc[i]! < 0) continue
              bits |= BRAILLE[dx]![dy]!
              if (fa[i]! > best) best = fa[i]!, color = fc[i]!
            }
          if (bits) {
            const bg = top === bot ? top : mix(top, bot, 0.5)
            out.push([String.fromCharCode(0x2800 + bits), mix(bg, color, Math.max(0.3, Math.min(1, best))), bg])
          } else out.push(top === bot ? [' ', top, top] : ['▀', top, bot])
        }
      return cells(out)
    },
  }
  return api
}

/** A hero's picture: its still cells, and (while something in it moves) the cells at time t in seconds. */
export type Picture = { cells: string; live: ((t: number) => string) | null }

const pictures = new Map<string, { still: Still; moving: (cv: Canvas, t: number) => void }>()
const lastFrame = new Map<string, { k: number; cells: string }>() // a moving picture's latest frame, one a picture
/** A picture drawn once per input and palette (memo), and its moving layer drawn over a copy of it at ≤10 fps. */
function picture(input: string, cols: number, rows: number, draw: (cv: Canvas) => ((cv: Canvas, t: number) => void) | null): Picture {
  const key = `${input} ${C.bg} ${C.accent} ${C.accent2} ${C.ok} ${C.edit} ${C.agent} ${C.warn}` // and every colour it takes
  let moving: ((cv: Canvas, t: number) => void) | null = null
  const still = memo(`pic ${key}`, () => {
    const cv = canvas(cols, rows)
    const m = draw(cv)
    if (m) {
      if (pictures.size >= 16) pictures.clear(), lastFrame.clear() // ponytail: a full clear; few pictures move at once
      pictures.set(key, { still: cv.copy(), moving: m })
    }
    return cv.cells()
  })
  const p = pictures.get(key)
  if (p) moving = p.moving
  if (!moving || !p) return { cells: still, live: null }
  return {
    cells: still,
    live: (t: number) => { // ≤10 fps: a frame per tenth of a second, drawn once
      const k = Math.floor(t * 10), was = lastFrame.get(key)
      if (was && was.k === k) return was.cells
      const cv = canvas(cols, rows, p.still)
      p.moving(cv, k / 10)
      const got = cv.cells()
      lastFrame.set(key, { k, cells: got })
      return got
    },
  }
}

// ── MERGE TRAIN (board.js's train Sankey): each waiting lane a box (its id on the top edge, its title inside, its
// steps along the bottom edge) pouring a curve into the gate, a funnel labelled above; out of the gate a band into
// foreman/build and what landed in the last day, and under the box the lanes landed, a ✓ and the time each.
export type MergeIn = { waiting: { id: string; title: string; color: number; frac: number }[]; landed: { id: string; at: string }[]
  landed24: number; running: boolean; branch?: string }
const words = (s: string, n: number) => {
  let out = ''
  for (const w of s.split(/\s+/).filter(Boolean).slice(0, 3)) {
    const next = out ? `${out} ${w}` : w
    if (next.length > n) break
    out = next
  }
  out = out.replace(/(?:\s+(?:the|a|an|of|to|in|on|for|and|with|by))+$/, '') // never end on a little word ('tidy the')
  return out // whole words or nothing: never "tid" for "tidy"
}
/** an ISO time as the viewer's local HH:MM ('' when it isn't one), as board.js's hm */
export const hm = (iso: string) => {
  const t = new Date(iso)
  return Number.isNaN(t.getTime()) ? '' : `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`
}

export function mergePicture(cols: number, rows: number, d: MergeIn): Picture {
  return picture(`merge ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, H = rows * 2, edge = mix(C.line, C.dim, 0.5)
    // right to left: the build's box as wide as its words, the gate three columns before it, the lanes the rest
    const name = fit(d.branch || 'build', W >= 40 ? 18 : 7) // T-4008: the branch it lands on, not foreman/build in every repo
    const said = W >= 40 ? `${d.landed24} landed · 24 h` : `${d.landed24} landed`
    const RW = clamp(Math.max(name.length, said.length) + 2, 9, 20), bx = W - RW
    const gw = clamp(Math.round(W * 0.14), 6, 14), gx1 = bx - 2, gx0 = gx1 - gw // the gate about as wide as it is tall
    const LW = clamp(gx0 - 4, 10, 30)
    // a lane's box: its id on the top edge, its title inside (two lines when one won't hold it), its steps the bottom
    const lines = d.waiting.some(l => narrow(l.title).length > LW - 2) ? 2 : 1, bh = lines + 2
    const n = d.waiting.length * bh <= rows ? d.waiting.length : Math.max(1, Math.floor((rows - 1) / bh))
    const more = d.waiting.length - n
    const top = Math.max(0, Math.floor((rows - n * bh - (more > 0 ? 1 : 0)) / 2))
    const cy = H / 2, fh = clamp(Math.max(gw + 2, n * 3 + 2), 6, H - 6), fr = Math.max(2, fh * 0.3) // tall mouth, short neck
    const g = { t0: cy - fh / 2, b0: cy + fh / 2, t1: cy - fr / 2, b1: cy + fr / 2 }
    // the gate: a funnel the lanes pour into, lit while a train runs, named above
    cv.trap(gx0, gx1, g.t0, g.b0, g.t1, g.b1, mix(C.bg, C.accent, d.running ? 0.32 : 0.18), 1, C.accent)
    const label = d.running ? 'gate · running' : 'gate'
    const lx = clamp(Math.round((gx0 + gx1) / 2 - label.length / 2), LW + 2, W - label.length)
    const beside = n > 0 && lx < LW + 4 // a label that would read on with a lane box's words goes above the boxes
    cv.write(lx, Math.max(0, beside ? Math.min(top - 1, Math.floor(g.t0 / 2) - 1) : Math.floor(g.t0 / 2) - 1), label, C.accent)
    const flows: { P: Bez; color: number }[] = []
    const inside = mix(C.bg, C.ink, 0.03)
    d.waiting.slice(0, n).forEach((l, i) => {
      const r = top + i * bh
      const ty = n > 1 ? g.t0 + 1 + (i * (fh - 2)) / (n - 1) : cy // each meets the mouth at its own height
      const P = link(1 + LW, (r + bh / 2) * 2, gx0, ty)
      cv.curve(P, l.color, 0.14, 2) // a faint band under …
      cv.fcurve(P, l.color, 0.85) // … a smooth hairline, as the reference's flows
      flows.push({ P, color: l.color })
      cv.box(1, r, LW, bh, edge, inside)
      cv.write(3, r, ` ${l.id} `, l.color, inside)
      wrap(l.title, LW - 2, lines).forEach((s, j) => cv.write(2, r + 1 + j, s, C.muted, inside))
      const bar = Math.round((LW - 2) * clamp(l.frac, 0, 1))
      for (let c = 0; c < LW - 2; c++) cv.write(2 + c, r + bh - 1, c < bar ? '━' : '─', c < bar ? l.color : edge, inside)
    })
    if (more > 0) cv.write(2, Math.min(rows - 1, top + n * bh), `+${more} more`, C.dim)
    if (!d.waiting.length) cv.write(1, Math.floor(rows / 2), words('no lanes waiting', gx0 - 2), C.dim) // T-4008: whole words, never 'waiti…'
    // out of the gate into the build: a band to the box, the lanes landed under it, newest first
    const L = d.landed.slice().reverse()
    const br = clamp(Math.floor(rows / 2) - 2, 0, Math.max(0, rows - 4 - Math.min(L.length || 1, 4)))
    const out = link(gx1, cy, bx, (br + 2) * 2)
    cv.curve(out, C.ok, 0.16, 2)
    cv.fcurve(out, C.ok, 0.85)
    cv.fcurve(shift(out, 0.5), C.ok, 0.7)
    const shelf = mix(C.bg, C.ok, 0.1)
    cv.box(bx, br, RW, 4, C.ok, shelf)
    cv.write(bx + 1, br + 1, name, C.ink, shelf)
    cv.write(bx + 1, br + 2, said, C.ok, shelf)
    const room = rows - br - 4
    const shown = L.length > room ? L.slice(0, Math.max(0, room - 1)) : L
    shown.forEach((x, i) => {
      const at = hm(x.at), r = br + 4 + i
      cv.write(bx, r, '✓', C.ok)
      cv.write(bx + 2, r, fit(x.id, RW - 3 - (at ? at.length + 1 : 0)), C.muted)
      if (at) cv.write(W - at.length, r, at, C.dim)
    })
    if (L.length > shown.length && room > 0) cv.write(bx + 2, br + 4 + shown.length, `+${L.length - shown.length} more`, C.dim)
    if (!L.length && room > 0) cv.write(bx, br + 4, RW >= 15 ? 'none landed yet' : 'none yet', C.dim)
    if (!d.running) return null
    return (live, t) => { // packets ride the hairlines into the gate, and out into the build
      flows.forEach((f, i) => {
        for (let j = 0; j < 2; j++) live.packet(...along(f.P, (t * 0.3 + i * 0.37 + j * 0.5) % 1), f.color)
      })
      for (let j = 0; j < 3; j++) live.packet(...along(out, (t * 0.45 + j / 3) % 1), C.ok)
    }
  })
}

// ── LANES (board.js's lane clusters): each lane a hub (ringed, glowing in its colour, its id as a pill above, its
// state and steps under), the files it holds as leaves on hairline spokes, each named; a dotted link joins two lanes'
// leaves for a file both hold (their hubs when a leaf isn't drawn); a building lane sends a packet down its spokes.
export type LaneIn = { id: string; title: string; color: number; done: number; total: number; files: string[]; state: string; next: boolean }
const TAU = Math.PI * 2
/** a file's name in n cells: whole, else without its extension, else cut with … */
const fileName = (f: string, n: number) => {
  const b = f.replace(/^.*\//, '')
  return b.length <= n ? b : b.replace(/\.[^.]+$/, '').length <= n ? b.replace(/\.[^.]+$/, '') : fit(b, n)
}

export function lanesPicture(cols: number, rows: number, lanes: LaneIn[]): Picture {
  return picture(`lanes ${cols}x${rows} ${JSON.stringify(lanes)}`, cols, rows, cv => {
    const W = cols
    if (!lanes.length) {
      cv.write(2, Math.floor(rows / 2), 'no builder lanes out', C.dim)
      return null
    }
    // the grid: the most columns (cells 18 wide at least) whose rows of 5 or more hold every lane, else as many as fit
    const maxc = Math.max(1, Math.min(lanes.length, Math.floor(W / 18)))
    let gc = maxc
    for (let k = maxc; k >= 1; k--) if (k * Math.floor(rows / 5) >= lanes.length) { gc = k; break }
    const cap = Math.max(1, gc * Math.max(1, Math.floor(rows / 5)))
    // lanes holding a file in common side by side, so the link between them runs level
    const order: LaneIn[] = []
    for (const l of lanes) {
      if (order.includes(l)) continue
      order.push(l)
      for (const m of lanes) if (!order.includes(m) && m.files.some(f => l.files.includes(f))) order.push(m)
    }
    const L = order.length > cap ? order.slice(0, Math.max(1, cap - 1)) : order
    const gr = Math.ceil(L.length / gc), cw = W / gc, ch = Math.floor(rows / gr)
    const holders = new Map<string, number>() // a file's lanes: the ones two lanes share come first, so their link shows
    for (const l of L) for (const f of new Set(l.files)) holders.set(f, (holders.get(f) ?? 0) + 1)
    // each lane's leaves (a row apart where they fit); a grid row as tall as its tallest lane, the rows centred together
    const plan = L.map(l => {
      const files = [...new Set(l.files)].sort((a, b) => (holders.get(b) ?? 0) - (holders.get(a) ?? 0))
      const slots = ch - 2, k = files.length > slots ? slots - 1 : files.length, extra = files.length - k
      const leaves = Math.max(1, k + (extra > 0 ? 1 : 0)), sp = slots >= 2 * leaves - 1 ? 2 : 1
      return { l, files, k, extra, sp, leafRows: (leaves - 1) * sp + 1 }
    })
    const rowH = Array.from({ length: gr }, (_, r) => Math.min(ch, 2 + Math.max(...plan.slice(r * gc, (r + 1) * gc).map(p => p.leafRows))))
    const used = rowH.reduce((a, b) => a + b, 0), air = gr > 1 ? clamp(Math.floor((rows - used) / gr), 0, 2) : 0
    const rowTop = rowH.map((_, r) => Math.floor((rows - used - air * (gr - 1)) / 2) + rowH.slice(0, r).reduce((a, b) => a + b, 0) + r * air)
    const leafAt = new Map<string, { x: number; y: number; end: number; row: number }[]>()
    const hubs: { x: number; y: number }[] = []
    const spokes: { P: Bez; color: number; phase: number }[][] = []
    plan.forEach(({ l, files, k, extra, sp, leafRows }, i) => {
      const row = Math.floor(i / gc), inRow = row === gr - 1 ? L.length - row * gc : gc
      const x0 = Math.round((W - inRow * cw) / 2 + (i - row * gc) * cw), w = Math.floor(cw) - 1
      const top = rowTop[row]!, hr = top + 1 + Math.floor((leafRows - 1) / 2)
      const hx = x0 + 2, hy = hr * 2 + 1
      hubs.push({ x: hx, y: hy })
      // the pill: its id, and the title's first words where they fit
      const pill = ` ${l.id} `, room = w - pill.length - 2
      cv.write(x0 + 1, top, pill, l.color, mix(C.bg, l.color, 0.2))
      const said = room >= 5 ? words(l.title.toLowerCase(), room) : ''
      if (said) cv.write(x0 + 2 + pill.length, top, said, C.muted)
      // the leaves: a node a file, a hairline spoke from the hub, its name beside it
      const lx = x0 + 7, own: { P: Bez; color: number; phase: number }[] = []
      files.slice(0, k).forEach((f, j) => {
        const lr = top + 1 + j * sp, ly = lr * 2 + 1
        const P = link(hx + 1, hy, lx - 0.5, ly)
        cv.fcurve(P, l.color, 0.75)
        own.push({ P, color: l.color, phase: j / Math.max(1, k) })
        const shared = (holders.get(f) ?? 0) > 1, name = fileName(f, x0 + w - lx - 2)
        for (const dx of [-0.25, 0.25]) for (const dy of [-0.25, 0.25]) cv.fdot(lx + dx, ly + dy, shared ? C.ink : l.color)
        cv.write(lx + 2, lr, name, shared ? C.ink : C.muted)
        leafAt.set(f, [...(leafAt.get(f) ?? []), { x: lx, y: ly, end: lx + 3 + name.length, row }])
      })
      if (extra > 0) cv.write(lx + 2, top + 1 + k * sp, `+${extra} more`, C.dim)
      if (!files.length) cv.write(lx, top + 1, fit('no files held', x0 + w - lx), C.dim)
      spokes.push(own)
      // the hub: a soft glow under a ringed node (the next lane to merge: white)
      cv.glow(hx, hy, 2.5, l.color, 0.35)
      cv.write(hx, hr, '◉', l.next ? 0xffffff : l.color)
      // under the hub: its state, its steps, a bar of them
      const sr = top + 1 + leafRows, st = `${l.state} ${l.done}/${l.total}`
      const bw = clamp(w - st.length - 2, 0, 8), lit = Math.round((bw * l.done) / Math.max(1, l.total))
      cv.write(x0 + 1, sr, fit(st, w), C.dim)
      for (let c = 0; c < bw; c++) cv.write(x0 + 2 + st.length + c, sr, c < lit ? '━' : '─', c < lit ? l.color : mix(C.line, C.dim, 0.4))
    })
    // two lanes holding one file: a dotted link from one's leaf to the other's, level when they sit side by side (their
    // hubs when a leaf isn't drawn)
    for (let i = 0; i < L.length; i++)
      for (let j = i + 1; j < L.length; j++)
        for (const f of new Set(L[i]!.files)) {
          if (!L[j]!.files.includes(f)) continue
          const at = leafAt.get(f) ?? []
          const [a, b] = [...at].sort((p, q) => p.x - q.x)
          if (a && b && a.row === b.row && a.y === b.y && a.end < b.x - 1) cv.fdashed(a.end, a.y, b.x - 1.5, b.y, C.muted)
          else cv.fdashed(hubs[i]!.x, hubs[i]!.y, hubs[j]!.x, hubs[j]!.y, C.muted, 0.7)
        }
    if (lanes.length > L.length) cv.write(W - `+${lanes.length - L.length} lanes`.length - 1, rows - 1, `+${lanes.length - L.length} lanes`, C.dim)
    const busy = L.map((l, i) => (l.state === 'building' ? spokes[i]! : [])).filter(s => s.length)
    if (!busy.length) return null
    return (live, t) => { // a building lane writes: a packet runs from its hub down one spoke after another
      busy.forEach((s, i) => {
        const u = t / 1.4 + i * 0.31, P = s[Math.floor(u) % s.length]!.P
        live.packet(...along(P, u % 1), s[0]!.color)
      })
    }
  })
}

// ── TASK: THE LOOP (board.js's LOOP): the words you asked, ❯❯❯ into the brief they became (its title, its criteria as
// [x]/[ ] lines, its steps as ✓/▸ lines), over its progress and the stage strip. Words, not bars.
export type TaskIn = { id: string; stage: string; stages: string[]; title: string; words: string; criteria: { text: string; ok: boolean }[];
  steps: { text: string; done: boolean; current: boolean }[]; files: { add: number; del: number }[]; done: number; total: number; time: string }

/** s wrapped at word breaks into at most `max` lines of `w` cells, the last one cut with … when more is left */
export function wrap(s: string, w: number, max: number): string[] {
  if (max <= 0 || w <= 0) return []
  const lines: string[] = []
  let line = ''
  for (const word of narrow(s).split(/\s+/).filter(Boolean)) {
    const next = line ? `${line} ${word}` : word
    if ([...next].length <= w) line = next
    else {
      if (line) lines.push(line)
      line = fit(word, w)
    }
  }
  if (line) lines.push(line)
  if (lines.length > max) {
    lines.length = max
    lines[max - 1] = fit(`${lines[max - 1]} …`, w)
  }
  return lines
}

export function taskPicture(cols: number, rows: number, d: TaskIn): Picture {
  return picture(`task ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, strip = rows >= 8 // the progress row and the stages under the cards
    const ch = Math.max(4, rows - (strip ? 2 : 0)) // the cards' height
    const edge = mix(C.line, C.dim, 0.5), pink = C.edit
    const wide = W >= 60
    // "you asked": a page with a hatched gutter, the words quoted
    let cx = 1
    if (wide) {
      const bw = clamp(Math.round(W * 0.34), 16, 40), paper = mix(C.bg, C.ink, 0.03), head = mix(C.bg, pink, 0.16)
      cv.box(1, 0, bw, ch, edge, paper)
      cv.rect(2, 2, bw - 2, 2, head)
      cv.write(3, 1, 'YOU ASKED', pink, head)
      for (let r = 2; r < ch - 1; r++) cv.write(2, r, '╱', mix(C.line, C.dim, 0.25), paper)
      wrap(`“${d.words || d.title}”`, bw - 6, ch - 4).forEach((s, i) => cv.write(4, 3 + i, s, C.muted, paper))
      const mid = Math.floor(ch / 2)
      for (let k = 0; k < 3; k++) cv.write(bw + 2 + k, mid, '❯', mix(C.bg, pink, 0.4 + 0.3 * k))
      cx = bw + 6
    }
    // the brief: id.md · STAGE on its title bar, the title, the criteria, the steps around the current one
    const cw = W - 1 - cx, paper = mix(C.bg, pink, 0.035), head = mix(C.bg, pink, 0.2)
    cv.box(cx, 0, cw, ch, mix(pink, C.bg, 0.45), paper)
    cv.rect(cx + 1, 2, cw - 2, 2, head)
    const tag = `${d.id}.md`
    cv.write(cx + 2, 1, tag, pink, head)
    cv.write(cx + 2 + tag.length, 1, fit(` · ${d.stage.toUpperCase()}`, Math.max(0, cw - tag.length - 4)), C.dim, head)
    const iw = cw - 4, last = ch - 2
    let r = 2
    if (!wide && d.words && d.words !== d.title && r <= last) cv.write(cx + 2, r++, fit(`“${d.words}”`, iw), C.muted, paper)
    for (const s of wrap(`# ${d.title}`, iw, 2)) if (r <= last) cv.write(cx + 2, r++, s, C.ink, paper)
    r++
    const room = Math.max(0, last - r + 1)
    let nc = d.criteria.length, ns = d.steps.length
    if (nc + ns + (nc && ns ? 1 : 0) > room) {
      nc = Math.min(nc, ns ? Math.ceil(room / 2) : room)
      ns = Math.max(0, room - nc - (nc ? 1 : 0))
    }
    const open = d.criteria.findIndex(c => !c.ok)
    d.criteria.slice(0, nc).forEach((c, k) => cv.write(cx + 2, r++, fit(`${c.ok ? '[x]' : '[ ]'} ${c.text}`, iw),
      c.ok ? C.ok : k === open ? C.accent : C.dim, paper))
    if (nc && ns) r++
    const cur = Math.max(0, d.steps.findIndex(s => s.current)), s0 = Math.max(0, Math.min(cur - 1, d.steps.length - ns))
    d.steps.slice(s0, s0 + ns).forEach((s, k) => cv.write(cx + 2, r++, fit(`${s.current ? '▸' : s.done ? '✓' : ' '} ${s0 + k + 1}. ${s.text}`, iw),
      s.current ? pink : s.done ? C.dim : C.muted, paper))
    if (!strip) return null
    // under the cards: the progress (and what changed, once anything has), then the stage strip with its names
    const add = d.files.reduce((t, f) => t + f.add, 0), del = d.files.reduce((t, f) => t + f.del, 0)
    const label = ` ${d.done}/${d.total} steps${d.time ? ` · ${d.time}` : ''}${d.files.length ? ` · ● ${d.files.length} file${d.files.length === 1 ? '' : 's'} +${add} −${del}` : ''}`
    const pr = rows - 2, bw = Math.max(4, W - label.length - 3)
    cv.rect(1, pr * 2 + 1, bw, 1, mix(C.line, C.dim, 0.3))
    cv.rect(1, pr * 2 + 1, Math.round((bw * d.done) / Math.max(1, d.total)), 1, pink, 0.9)
    cv.write(1 + bw + 1, pr, label, C.muted)
    // the stages as words: those passed pink, the one it stands at a pill, those to come dim; cut round it to fit
    const stages = ['captured', ...d.stages.filter(s => s !== 'captured')], si = stages.indexOf(d.stage)
    const word = (i: number) => (i === si ? ` ${stages[i]} ` : stages[i]!)
    let lo = 0, hi = stages.length - 1
    const len = () => Array.from({ length: hi - lo + 1 }, (_, k) => word(lo + k)).join(' · ').length + (lo ? 2 : 0) + (hi < stages.length - 1 ? 2 : 0)
    while (len() > W - 2 && hi > lo) si - lo > hi - si ? lo++ : hi--
    let x = 1
    const put = (s: string, fg: number, bg?: number) => (cv.write(x, rows - 1, s, fg, bg), (x += s.length))
    if (lo) put('‹ ', C.dim)
    for (let i = lo; i <= hi; i++) {
      if (i > lo) put(' · ', C.dim)
      if (i === si) put(word(i), C.on, pink)
      else put(word(i), i < si ? mix(C.bg, pink, 0.7) : C.dim)
    }
    if (hi < stages.length - 1) put(' ›', C.dim)
    return null
  })
}

// ── CPU (board.js's CPU): the cores busy now as a big figure (a pixel font), its share against the goal, and the kinds
// of work (tests, claude, models, hooks, other: `sampo cpu`'s) each a colour with its cores; beside them a stacked bar a
// minute of the last 30, the goal a dashed line that marches, the newest bar's top breathing.
export type CpuIn = { cores: number; goal: number; kinds: string[]; now: Record<string, number>; minutes: number[][] }
const KIND_C = (k: string) => (({ tests: C.accent, claude: C.edit, models: C.accent2, hooks: C.agent }) as Record<string, number>)[k] ?? mix(C.dim, C.bg, 0.25)
// a 3×5 pixel font for the big figure: a row a digit's five lines, each three bits
const GLYPH: Record<string, number[]> = {
  '0': [7, 5, 5, 5, 7], '1': [2, 6, 2, 2, 7], '2': [7, 1, 7, 4, 7], '3': [7, 1, 7, 1, 7], '4': [5, 5, 7, 1, 1],
  '5': [7, 4, 7, 1, 7], '6': [7, 4, 7, 5, 7], '7': [7, 1, 1, 2, 2], '8': [7, 5, 7, 5, 7], '9': [7, 5, 7, 1, 7], '.': [0, 0, 0, 0, 1],
}
/** s in the pixel font at (x, y), each glyph a pixel apart; its width in pixels */
const big = (cv: Canvas, x: number, y: number, s: string, color: number) => {
  let at = x
  for (const ch of s) {
    const g = GLYPH[ch]
    if (!g) continue
    const w = ch === '.' ? 1 : 3
    g.forEach((bits, j) => {
      for (let i = 0; i < w; i++) if (bits & (1 << (w - 1 - i))) cv.dot(at + i, y + j, color)
    })
    at += w + 1
  }
  return at - x - 1
}

export function cpuPicture(cols: number, rows: number, d: CpuIn): Picture {
  return picture(`cpu ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, H = rows * 2
    const busy = d.now.busy ?? d.kinds.reduce((t, k) => t + (d.now[k] ?? 0), 0)
    const share = d.cores ? (100 * busy) / d.cores : 0, over = share > d.goal, tone = over ? C.warn : C.ok
    const figure = busy.toFixed(1), pct = `${share.toFixed(share < 10 ? 1 : 0)}%`
    const lines = [`cores busy of ${d.cores}`, `${pct} · ${over ? 'over' : 'under'} goal`]
    const side = W >= 40 ? Math.max(...lines.map(s => s.length)) + 2 : 0
    let r0 = 0 // the chart's first row
    if (side) { // the figure, what it is, the goal; the kinds with their cores
      big(cv, 1, 1, figure, C.ink)
      cv.write(1, 3, lines[0]!, C.dim)
      cv.write(1, 4, lines[1]!, tone)
      d.kinds.slice(0, Math.max(0, rows - 7)).forEach((k, i) => {
        const v = (d.now[k] ?? 0).toFixed(1)
        cv.write(1, 6 + i, '■', KIND_C(k))
        cv.write(3, 6 + i, fit(k, side - v.length - 5), C.muted)
        cv.write(side - 1 - v.length, 6 + i, v, C.ink)
      })
    } else { // narrow: the figure in words over the chart
      cv.write(1, 0, fit(`${figure} cores busy · ${pct} ${over ? 'over' : 'under'} goal`, W - 2), tone)
      r0 = 1
    }
    // the bars: one a minute, the newest at the right; a gutter on the right for the scale and the goal's mark
    const gut = 4, cx = side + 1, cw = W - cx - gut - 1, top = r0 * 2 + 1, bot = (rows - 1) * 2 - 1
    if (cw < 6 || bot - top < 6) return null
    const n = Math.min(30, cw), per = cw / n, bw = per >= 2 ? Math.floor(per) - 1 : 1
    const M = d.minutes.slice(-n), goal = (d.cores * d.goal) / 100
    const tops = M.map(r => r.slice(1).reduce((t, v) => t + v, 0))
    const peak = Math.max(goal * 1.6, ...tops, 0.1) * 1.12, yOf = (v: number) => bot - ((bot - top) * v) / peak
    for (let y = top; y <= bot; y++) cv.dot(cx - 1, y, mix(C.bg, C.line, 0.6)) // the axis
    const newest = M.at(-1)?.[0] ?? 0
    M.forEach(r => {
      const k = Math.round((newest - r[0]!) / 60) // minutes before the newest: a gap stays a gap
      if (k < 0 || k >= n) return
      const x = Math.round(cx + (n - 1 - k) * per)
      let acc = 0
      d.kinds.forEach((k, i) => { // stacked from the bottom, edges rounded on the running total: no gaps, no overlap
        const v = r[i + 1] ?? 0
        if (v <= 0) return
        const y1 = Math.round(yOf(acc)), y0 = Math.round(yOf(acc + v))
        acc += v
        if (y1 > y0) cv.rect(x, y0 + 1, bw, y1 - y0, KIND_C(k), k === 'other' ? 0.6 : 0.9)
      })
    })
    const gy = Math.round(yOf(goal))
    cv.write(W - gut, Math.floor(gy / 2), `${d.goal}%`, tone)
    const scale = peak.toFixed(peak < 10 ? 1 : 0)
    if (Math.floor(gy / 2) > r0 + 1) cv.write(W - gut, r0, scale, C.dim)
    const span = `−${n} min`
    cv.write(cx, rows - 1, span, C.dim)
    cv.write(cx + cw - 3, rows - 1, 'now', C.dim)
    const lastX = Math.round(cx + (n - 1) * per), lastTop = M.length ? Math.round(yOf(tops.at(-1)!)) : bot
    return (live, t) => { // the goal line marches, the newest minute's top breathes
      const off = Math.floor(t * 4) % 4
      for (let x = cx; x < cx + cw; x++) if ((x + off) % 4 < 2) live.dot(x, gy, tone)
      if (M.length) live.rect(lastX, lastTop, bw, 1, mix(C.ink, C.bg, 0.35 + 0.35 * Math.sin(t * 2.4)))
    }
  })
}

// ── MODELS: who calls which model (board.js's MODELS Sankey). A caller a tinted row on the left over a bar of its
// calls; a model a row on the right under its family (CLAUDE, JEFF, OLLAMA, each its colour) with its calls; a curve a
// caller and a model, as thick as its calls; packets ride the curves that carried calls in the last minute.
export type ModelsIn = { links: { from: string; to: string; n: number; hot: number }[] }
const GROUPS = ['claude', 'jeff', 'ollama'] as const
const family = (name: string) => (/^(claude|opus|sonnet|haiku)/i.test(name) ? 'claude' : /^jeff/i.test(name) ? 'jeff' : 'ollama')
/** s in at most n cells, by code point (a cut never splits a surrogate pair) */
const fit = (s: string, n: number) => {
  const a = [...narrow(s)]
  return a.length > n ? `${a.slice(0, Math.max(0, n - 1)).join('')}…` : a.join('')
}

export function modelsPicture(cols: number, rows: number, d: ModelsIn): Picture {
  return picture(`models ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, gc = { claude: C.warn, jeff: C.agent, ollama: C.accent2 }
    const tint = [C.accent2, C.ok, C.accent, C.edit, C.agent, C.warn] // each caller its own colour, as the reference's
    const byKey = new Map<string, { a: string; m: string; n: number; hot: number }>()
    for (const l of d.links) { // a caller is its first two words (board.js's agent): `cost --x` and `cost` are one
      const a = l.from.split(/\s--?/)[0]!.split(' ').slice(0, 2).join(' '), k = `${a}\u0000${l.to}`
      const was = byKey.get(k) ?? { a, m: l.to, n: 0, hot: 0 }
      byKey.set(k, { ...was, n: was.n + l.n, hot: was.hot + l.hot })
    }
    const links = [...byKey.values()].sort((x, y) => y.n - x.n)
    if (!links.length) {
      cv.write(2, Math.floor(rows / 2), 'no model calls in the last five minutes', C.dim)
      return null
    }
    const per = new Map<string, number>()
    for (const l of links) per.set(l.a, (per.get(l.a) ?? 0) + l.n)
    const agents = [...per.keys()].slice(0, Math.max(1, Math.min(8, Math.floor(rows / 2))))
    let models = [...new Set(links.filter(l => agents.includes(l.a)).map(l => l.m))]
      .map(m => ({ m, g: family(m), n: links.filter(l => l.m === m).reduce((t, l) => t + l.n, 0) }))
      .sort((x, y) => GROUPS.indexOf(x.g) - GROUPS.indexOf(y.g) || y.n - x.n)
    const tall = () => new Set(models.map(x => x.g)).size + models.length * 2 - 1 // a family's heading, then a row a model
    while (models.length > 1 && tall() > rows) models = models.slice(0, -1)
    const aw = clamp(Math.round(W * 0.24), 10, 24), mw = clamp(Math.round(W * 0.36), 16, 28), mx = W - mw - 1 // a model's name whole
    // the callers, centred: a pill (the name) over a bar of its calls
    const pA = clamp(Math.floor(rows / agents.length), 2, 3), topA = Math.max(0, Math.floor((rows - ((agents.length - 1) * pA + 2)) / 2))
    const maxA = Math.max(1, ...agents.map(a => per.get(a)!))
    const A = new Map(agents.map((a, i) => {
      const r = topA + i * pA, k = tint[i % tint.length]!, ground = mix(C.bg, k, 0.16)
      cv.rect(1, r * 2, aw, 2, ground)
      cv.write(2, r, fit(a, aw - 2), C.ink, ground)
      cv.rect(1, (r + 1) * 2, Math.max(2, Math.round((aw * per.get(a)!) / maxA)), 1, k, 0.85)
      return [a, r * 2 + 1] as const
    }))
    // the models, centred under their families
    let r = Math.max(0, Math.floor((rows - tall()) / 2))
    const M = new Map<string, { y: number; k: number }>()
    for (const g of GROUPS) {
      const of = models.filter(x => x.g === g)
      if (!of.length) continue
      cv.write(mx, r++, g.toUpperCase(), gc[g])
      for (const x of of) {
        const ground = mix(C.bg, gc[g], 0.14), n = String(x.n)
        cv.rect(mx, r * 2, mw, 2, ground)
        cv.rect(mx, r * 2, 1, 2, gc[g])
        cv.write(mx + 2, r, fit(x.m, mw - n.length - 4), C.ink, ground)
        cv.write(mx + mw - 1 - n.length, r, n, C.dim, ground)
        M.set(x.m, { y: r * 2 + 1, k: gc[g] })
        r += 2
      }
    }
    // the curves, as thick as their calls
    const maxN = Math.max(1, ...links.map(l => l.n))
    const moving: { P: Bez; k: number; n: number; v: number; ph: number }[] = []
    for (const l of links) {
      const y0 = A.get(l.a), m = M.get(l.m)
      if (y0 === undefined || !m) continue
      const P = link(aw + 2, y0, mx - 2, m.y), f = Math.log(1 + l.n) / Math.log(1 + maxN)
      for (let k = 0, w = 1 + Math.round(3 * f); k < w; k++) // hairlines side by side, half a pixel apart: as thick as its calls
        cv.fcurve(shift(P, (k - (w - 1) / 2) * 0.5), m.k, 0.8)
      cv.packet(aw + 1.5, y0, m.k)
      cv.packet(mx - 1.5, m.y, m.k)
      if (l.hot) moving.push({ P, k: m.k, n: Math.min(3, 1 + Math.floor(Math.log2(1 + l.hot) / 2)), v: 0.16 + 0.14 * f, ph: seed(`${l.a},${l.m}`)() })
    }
    if (!moving.length) return null
    return (live, t) => { // packets on the curves that carried calls in the last minute
      for (const l of moving)
        for (let j = 0; j < l.n; j++) live.packet(...along(l.P, (t * l.v + l.ph + j / l.n) % 1), l.k)
    }
  })
}

// ── a quiet panel: its words, dim, in the middle
export function notePicture(cols: number, rows: number, lines: string[]): Picture {
  return picture(`note ${cols}x${rows} ${JSON.stringify(lines)}`, cols, rows, cv => {
    const out = lines.flatMap((s, i) => wrap(s, cols - 2, 3).map(t => [t, i ? C.dim : C.muted] as const)) // whole words, a line each
    const top = Math.max(0, Math.floor((rows - out.length) / 2))
    out.forEach(([t, color], i) => cv.write(Math.max(1, Math.floor((cols - [...t].length) / 2)), top + i, t, color))
    return null
  })
}

// ── LIBRARY: a dotted globe (board.js's): a dot per kept card or moon (one per several past the lattice), each grid
// its colour, the dots it doesn't fill faint; parallels and meridians as hairlines; over a kept-per-hour histogram.
export type LibraryIn = { counts: { cards: number; ui: number; moons: number; calling: number }; hourly: number[] }
const GRIDS = ['cards', 'ui', 'moons', 'calling'] as const
const TILT = 0.42

export function libraryPicture(cols: number, rows: number, d: LibraryIn): Picture {
  return picture(`library ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, H = rows * 2, col = { cards: C.accent2, ui: C.accent, moons: C.agent, calling: C.edit }
    const hb = (rows - 1) * 2, ht = hb - Math.max(4, Math.round(H * 0.18)) // the histogram's pixel rows
    const R = clamp(Math.min((ht - 2) / 2, W * 0.32), 4, 30), gx = W - R - 3, gy = 1 + R
    cv.glow(gx, gy, R * 1.3, C.accent2, 0.1)
    for (let k = 0; k < 160; k++) cv.fdot(gx + R * Math.cos((k * TAU) / 160), gy + R * Math.sin((k * TAU) / 160), C.accent2, 0.7)
    const ct = Math.cos(TILT), st = Math.sin(TILT), spin = 0.5
    for (const lat of [-60, -30, 0, 30, 60]) { // the parallels, their front halves
      const f = (lat * Math.PI) / 180
      for (let k = 0; k < 90; k++) {
        const a = (k * TAU) / 90, x = Math.cos(f) * Math.sin(a), y = Math.sin(f), z = Math.cos(f) * Math.cos(a)
        if (y * st + z * ct > 0) cv.fdot(gx + R * x, gy - R * (y * ct - z * st), C.accent2, 0.3)
      }
    }
    for (let m = 0; m < 6; m++) { // the meridians every 30°, their front halves
      const a = (m * Math.PI) / 6 + spin
      for (let j = 0; j <= 40; j++) {
        const f = -Math.PI / 2 + (j * Math.PI) / 40, y = Math.sin(f), r = Math.cos(f), z = r * Math.cos(a)
        if (y * st + z * ct > 0) cv.fdot(gx + R * r * Math.sin(a), gy - R * (y * ct - z * st), C.accent2, 0.3)
      }
    }
    // the lattice: N points on the sphere (a Fibonacci lattice), each grid a band of them as large as its share
    const N = clamp(Math.round(R * R * 1.6), 60, 600), total = GRIDS.reduce((t, k) => t + Math.max(0, d.counts[k]), 0)
    const scale = total ? Math.min(1, (N - GRIDS.length) / total) : 0
    const bands: { to: number; color: number }[] = []
    let at = 0
    for (const k of GRIDS) if (d.counts[k] > 0) bands.push({ to: (at += Math.max(1, Math.floor(d.counts[k] * scale))), color: col[k] })
    for (let i = 0; i < N; i++) {
      const y = 1 - (2 * (i + 0.5)) / N, r = Math.sqrt(1 - y * y), a = i * 2.39996323 + spin
      const x = Math.cos(a) * r, z = Math.sin(a) * r, depth = y * st + z * ct
      if (depth < -0.1) continue
      const band = bands.find(b => i < b.to)
      cv.fdot(gx + R * x, gy - R * (y * ct - z * st), band ? band.color : C.line, band ? (depth > 0.5 ? 1 : 0.62) : 0.5)
    }
    // the legend, top left
    GRIDS.forEach((k, i) => {
      if (i >= Math.floor(ht / 2) - 1) return
      cv.write(1, i, '■', col[k])
      cv.write(3, i, `${k} ${d.counts[k].toLocaleString('en')}`, C.muted)
    })
    // the histogram: kept per hour, the last day, the newest hour lit
    const hours = d.hourly.slice(-24), peak = Math.max(1, ...hours), bw = (W - 2) / 24
    hours.forEach((v, i) => {
      const h = Math.round(((hb - ht) * v) / peak)
      if (h) cv.rect(1 + Math.round(i * bw), hb - h + 1, Math.max(1, Math.round(bw) - 1), h, i === hours.length - 1 ? C.ink : C.accent2, 0.4 + (0.5 * i) / 23)
    })
    cv.write(1, rows - 1, '−24 h', C.dim)
    cv.write(W - 4, rows - 1, 'now', C.dim)
    const label = `kept / hour · peak ${peak}`
    if (W > label.length + 14) cv.write(Math.floor((W - label.length) / 2), rows - 1, label, C.dim)
    if (!total) cv.write(Math.max(0, Math.round(gx - 8)), Math.round(gy / 2), 'nothing kept yet', C.dim)
    return null
  })
}

// ── SAMPO: a terminal line with the newest sampo command run here (its caret), over the queue as a table with status
// pills, as board.js's; more than its rows hold: the last row says how many more.
export type SampoIn = { cmds: { cmd: string; ok: boolean }[]; queue: { id: string; status: string; kind: string; title: string }[]; queued: number }
const STATUS_C = () => ({ active: C.ok, planned: C.accent2, open: C.accent2, ready: C.accent, blocked: C.edit, parked: C.agent, waits: C.warn }) as Record<string, number>

export function sampoPicture(cols: number, rows: number, d: SampoIn): Picture {
  return picture(`sampo ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, edge = mix(C.line, C.dim, 0.5), term = mix(C.bg, C.ink, 0.03)
    // the terminal: the newest commands, as many as the queue's rows spare (one at least), the last with the caret
    const k = clamp(Math.min(d.cmds.length, rows - 6 - Math.min(d.queue.length, rows - 6)), 1, 5), shown = d.cmds.slice(-k)
    cv.box(0, 0, W, k + 2, edge, term)
    if (W >= 30) cv.write(W - 11, 0, ' sampo cli ', C.dim, term)
    shown.forEach((c, i) => {
      const last = i === shown.length - 1, cmd = fit(c.cmd, W - 8)
      cv.write(2, 1 + i, '$', last ? C.accent2 : C.dim, term)
      cv.write(4, 1 + i, cmd, !c.ok ? C.err : last ? C.ink : C.muted, term)
      if (last) cv.write(5 + [...cmd].length, 1 + i, '▌', C.edit, term)
    })
    if (!shown.length) cv.write(4, 1, 'waiting for a sampo command', C.dim, term)
    // the queue: a header on violet, a row an item: its id, its status as a pill, its kind, its title
    const head = mix(C.bg, C.agent, 0.16), tr = k + 3
    cv.rect(0, tr * 2, W, 2, head)
    const cId = 1, cSt = 9, cKind = 20, cTitle = W >= 46 ? 27 : 20
    cv.write(cId, tr, 'ID', C.muted, head)
    cv.write(cSt, tr, 'status', C.muted, head)
    if (cTitle > cKind) cv.write(cKind, tr, 'kind', C.muted, head)
    cv.write(cTitle, tr, 'title', C.muted, head)
    const room = rows - tr - 1, sc = STATUS_C(), total = Math.max(d.queued, d.queue.length)
    const rowsOf = d.queue.slice(0, total > room ? Math.max(0, room - 1) : room)
    rowsOf.forEach((q, i) => {
      const r = tr + 1 + i, k = sc[q.status] ?? C.dim, st = fit(q.status, 9)
      cv.write(cId, r, q.id, C.ink)
      cv.write(cSt, r, ` ${st} `, k, mix(C.bg, k, 0.22))
      if (cTitle > cKind) cv.write(cKind, r, fit(q.kind, 6), C.dim)
      cv.write(cTitle, r, fit(q.title, W - cTitle - 1), C.muted)
    })
    if (!d.queue.length) cv.write(cId, tr + 1, 'the queue is empty', C.dim)
    else if (total > rowsOf.length && room > 0) cv.write(cId, tr + 1 + rowsOf.length, `+${total - rowsOf.length} more`, C.dim)
    return null
  })
}

// ── STATE: the state folder as a tree (the newest entry lit), each entry's size and time as board.js's, as wide as its
// names need; beside it the latest decision as a card, titled with its file and date.
export type StateIn = { tree: { name: string; dir: boolean; n: number; kb: number; at?: string }[]; newest: string
  doc: { kind: string; title: string; text: string; at: string } | null }

export function statePicture(cols: number, rows: number, d: StateIn): Picture {
  return picture(`state ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, E = d.tree.slice(0, rows - 1)
    const size = (e: StateIn['tree'][number]) => (e.dir ? `${e.n}` : e.kb >= 1024 ? `${(e.kb / 1024).toFixed(1)} MB` : `${e.kb} kB`)
    const label = (e: StateIn['tree'][number]) => (e.dir ? `${e.name}/` : e.name)
    const nameW = Math.min(18, Math.max(6, ...E.map(e => label(e).length))), sizeW = Math.max(0, ...E.map(e => size(e).length))
    const timeW = E.some(e => hm(e.at ?? '')) ? 5 : 0
    // the tree: a marker, a glyph, the name whole, then its time (and its size where the card still keeps 18 columns)
    const base = 4 + nameW + (timeW ? timeW + 1 : 0), CARD = 18
    const withSize = W - (base + sizeW + 1) - 2 >= CARD, tw = Math.min(W - 1, base + (withSize ? sizeW + 1 : 0))
    const wide = W - tw - 2 >= CARD
    const TW = wide ? tw : W - 1
    cv.write(1, 0, 'state/', C.accent2)
    E.forEach((e, i) => {
      const r = 1 + i, hot = e.name === d.newest, ground = hot ? mix(C.bg, C.edit, 0.18) : undefined
      if (hot) cv.rect(1, r * 2, TW - 1, 2, ground!)
      cv.write(1, r, hot ? '▌' : ' ', C.edit, ground)
      cv.write(2, r, e.dir ? '▸' : '·', e.dir ? C.accent2 : C.dim, ground)
      const at = hm(e.at ?? ''), right = [withSize || !wide ? size(e) : '', at].filter(Boolean).join(' ')
      cv.write(4, r, fit(label(e), TW - 4 - (right ? right.length + 1 : 0)), hot ? C.ink : e.dir ? C.muted : C.dim, ground)
      if (right) cv.write(TW - right.length, r, right, C.dim, ground)
    })
    if (!d.tree.length) cv.write(1, 1, "the state folder isn't read yet", C.dim)
    if (!wide) return null
    // the card: the latest decision as the file it lives in, its date, its title and why
    const dx = TW + 1, dw = W - dx, paper = mix(C.bg, C.edit, 0.035), head = mix(C.bg, C.edit, 0.2)
    cv.box(dx, 0, dw, rows, mix(C.edit, C.bg, 0.45), paper)
    // its head: the file, and the date beside it, or under it when the two won't share a line
    const file = `${d.doc?.kind || 'decision'}s.md`, date = (d.doc?.at ?? '').slice(0, 10)
    const below = !!date && file.length + date.length + 5 > dw
    cv.rect(dx + 1, 2, dw - 2, below ? 4 : 2, head)
    cv.write(dx + 2, 1, fit(file, dw - 4), C.edit, head)
    if (date) cv.write(below ? dx + 2 : dx + dw - 2 - date.length, below ? 2 : 1, fit(date, dw - 4), C.dim, head)
    let r = below ? 4 : 3
    if (!d.doc) {
      cv.write(dx + 2, r, fit('no decision yet', dw - 4), C.dim, paper)
      return null
    }
    for (const s of wrap(`# ${d.doc.title}`, dw - 4, 3)) cv.write(dx + 2, r++, s, C.ink, paper) // the title whole where three lines hold it
    r++
    for (const s of wrap(d.doc.text, dw - 4, Math.max(0, rows - 1 - r))) cv.write(dx + 2, r++, s, C.muted, paper)
    return null
  })
}

// ── TESTS: a tile a test module, coloured by how it stands (cached green, running yellow, failing red, idle an outline),
// the tiles sized to fill the panel; a count of each above; running tiles pulse.
export type TestsIn = { modules: { name: string; state: string }[]; source: string }

export function testsPicture(cols: number, rows: number, d: TestsIn): Picture {
  return picture(`tests ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, H = rows * 2
    const col = { cached: mix(C.bg, C.ok, 0.45), running: C.accent, fail: C.err, idle: mix(C.bg, C.line, 0.95) } as Record<string, number>
    const STATES = ['cached', 'running', 'fail', 'idle'] as const
    const count = (k: string) => d.modules.filter(m => m.state === k).length
    // the legend: every state there is, its number bright, its word dim; tighter, then numbers alone, when it won't fit
    const items = STATES.filter(k => count(k))
    const legend = (gap: number, said: boolean) => items.map(k => `■ ${count(k)}${said ? ` ${k}` : ''}`).join(' '.repeat(gap)).length
    const [gap, said] = ([[3, true], [2, true], [1, true]] as const).find(([g, w]) => legend(g, w) <= W - 2) ?? [2, false]
    let lx = 1
    for (const k of items) {
      const n = `${count(k)}`
      cv.write(lx, 0, k === 'idle' ? '□' : '■', k === 'idle' ? mix(C.line, C.dim, 0.5) : col[k]!)
      cv.write(lx + 2, 0, n, C.ink)
      if (said) cv.write(lx + 3 + n.length, 0, k, C.dim)
      lx += 2 + n.length + (said ? k.length + 1 : 0) + gap
    }
    if (!d.modules.length) {
      cv.write(1, Math.floor(rows / 2), 'no test run seen yet', C.dim)
      return null
    }
    // the tiles: the grid whose tiles are the largest and squarest, stretched to fill the width and the height
    const x0 = 1, y0 = 3, gw = W - 2, gh = H - 2 - y0 - 1, n = d.modules.length
    let best = { c: 1, r: n, s: -1 }
    for (let c = 1; c <= n; c++) {
      const r = Math.ceil(n / c), g = 1
      const w = (gw - (c - 1) * g) / c, h = (gh - (r - 1) * g) / r
      if (w < 1 || h < 1) continue
      const s = Math.min(w, h) + Math.min(w, h) / Math.max(w, h) // the larger and the squarer, the better
      if (s > best.s) best = { c, r, s }
    }
    const fits = best.s < 0 ? Math.max(1, gw * Math.floor(gh)) : n
    const c = best.s < 0 ? gw : best.c, r = Math.ceil(Math.min(n, fits) / c), g = best.s < 0 ? 0 : 1
    const xs = Array.from({ length: c + 1 }, (_, i) => x0 + Math.round((i * (gw + g)) / c)) // edges: spread to fill
    const ys = Array.from({ length: r + 1 }, (_, j) => y0 + Math.round((j * (gh + g)) / r))
    const run: [number, number, number, number][] = []
    d.modules.slice(0, fits).forEach((m, i) => {
      const a = i % c, b = Math.floor(i / c), x = xs[a]!, y = ys[b]!, w = xs[a + 1]! - x - g, h = ys[b + 1]! - y - g
      if (m.state === 'idle') { // an outline, as board.js's
        cv.rect(x, y, w, h, mix(C.bg, C.line, 0.35))
        cv.rect(x, y, w, 1, col.idle!), cv.rect(x, y + h - 1, w, 1, col.idle!), cv.rect(x, y, 1, h, col.idle!), cv.rect(x + w - 1, y, 1, h, col.idle!)
      } else cv.rect(x, y, w, h, col[m.state] ?? col.idle!)
      if (m.state === 'running') run.push([x, y, w, h])
    })
    const bad = d.modules.filter(m => m.state === 'fail').map(m => m.name.replace(/^test_/, ''))
    const now = d.modules.filter(m => m.state === 'running').map(m => m.name.replace(/^test_/, ''))
    const line = bad.length ? `✗ ${bad.join(' ')}` : now.length ? `▸ ${now.join(' ')}` : d.source
    cv.write(1, rows - 1, fit(line, W - 2 - (n > fits ? 8 : 0)), bad.length ? C.err : now.length ? C.accent : C.dim)
    if (n > fits) cv.write(W - 1 - `+${n - fits}`.length, rows - 1, `+${n - fits}`, C.dim)
    if (!run.length) return null
    return (live, t) => { // the running tiles pulse
      const a = 0.45 + 0.55 * Math.abs(Math.sin(t * 3))
      for (const [x, y, w, h] of run) live.rect(x, y, w, h, mix(C.bg, C.accent, a))
    }
  })
}

// ── SESSIONS: every live Claude Code session on this machine, any project, as nodes orbiting the machine's core:
// each a ring gauge of its context in its project's colour, its task as a pill and its model beside it (outward, so
// the words never land on the orbit's other nodes); idle ones dim.
export type SessionsIn = { sessions: { id: string; project: string; color: number; task: string; model: string; pct: number | null; idle: boolean }[] }

export function sessionsPicture(cols: number, rows: number, d: SessionsIn): Picture {
  return picture(`sessions ${cols}x${rows} ${JSON.stringify(d)}`, cols, rows, cv => {
    const W = cols, H = (rows - 1) * 2 // the last row: the projects' legend
    const cx = W / 2, cy = H / 2, S = d.sessions.slice(0, 8)
    const rx = Math.max(6, W * 0.17), ry = Math.max(3, H * 0.36) // the orbit: narrow enough for words either side
    const ringC = mix(C.dim, C.accent2, 0.3)
    for (let k = 0; k < 160; k++) if (k % 4) cv.fdot(cx + rx * Math.cos((k * TAU) / 160), cy + ry * Math.sin((k * TAU) / 160), ringC, 0.5)
    for (let k = 0; k < 90; k++) if (k % 3 === 0) cv.fdot(cx + rx * 0.45 * Math.cos((k * TAU) / 90), cy + ry * 0.45 * Math.sin((k * TAU) / 90), ringC, 0.35)
    // the core: the machine, lit
    cv.glow(cx, cy, 5, C.accent2, 0.3)
    for (let y = -1; y <= 1; y++) for (let x = -2; x <= 2; x++) if ((x * x) / 6 + (y * y) / 2.5 <= 1) cv.dot(cx + x, cy + y, mix(C.line, C.accent2, 0.5))
    cv.packet(cx, cy, C.ink)
    if (!S.length) cv.write(Math.max(0, Math.round(cx - 8)), Math.min(rows - 2, Math.round(cy / 2) + 2), 'no live sessions', C.dim)
    // the sessions spread round the orbit from the top; words go outward: left of a left node, right of a right one
    const at = S.map((s, i) => {
      const a = -Math.PI / 2 + ((i + 0.5) * TAU) / S.length
      return { s, x: cx + rx * Math.cos(a), y: cy + ry * Math.sin(a) }
    })
    for (const { s, x, y } of at) {
      const k = s.idle ? 0.45 : 1, hot = (s.pct ?? 0) >= 80 ? C.warn : s.color
      cv.fline(cx, cy, x, y, s.color, 0.22 * k) // its tie to the machine
      const lit = Math.round(28 * clamp((s.pct ?? 0) / 100, 0, 1))
      for (let q = 0; q < 28; q++) { // the context gauge: lit clockwise from the top as far as its context is used
        const t = -Math.PI / 2 + (q * TAU) / 28
        cv.fdot(x + 2.8 * Math.cos(t), y + 2.4 * Math.sin(t), q < lit ? hot : mix(C.line, C.dim, 0.5), (q < lit ? 1 : 0.55) * k)
      }
      cv.glow(x, y, 3.5, s.color, 0.25 * k)
      if (s.idle) cv.fdot(x, y, s.color, 0.5)
      else cv.packet(x, y, s.color)
    }
    const taken = new Set<string>()
    for (const { s, x, y } of at) {
      const pill = ` ${fit(s.task || 'no task', 10)} `, under = `${fit(s.model, 12)}${s.pct !== null ? ` ${Math.round(s.pct)}%` : ''}`
      const left = x < cx - 0.5, r0 = clamp(Math.round(y / 2) - 1, 0, rows - 3)
      const col = (w: number) => (left ? Math.max(1, Math.round(x - 4) - w) : Math.min(W - w - 1, Math.round(x + 4))) // a cell off the edge
      const free = (c0: number, r: number, w: number) => !Array.from({ length: w }, (_, i) => `${r}:${c0 + i}`).some(q => taken.has(q))
      for (const r of [r0, r0 + 1, r0 - 1]) {
        if (r < 0 || r + 1 >= rows - 1) continue
        const pc = col(pill.length), uc = col(under.length)
        if (!free(pc, r, pill.length) || !free(uc, r + 1, under.length)) continue
        for (let i = 0; i < pill.length; i++) taken.add(`${r}:${pc + i}`)
        for (let i = 0; i < under.length; i++) taken.add(`${r + 1}:${uc + i}`)
        cv.write(pc, r, pill, s.idle ? C.dim : s.color, mix(C.bg, s.color, s.idle ? 0.1 : 0.22))
        cv.write(uc, r + 1, under, s.idle ? mix(C.dim, C.bg, 0.3) : C.muted)
        break
      }
    }
    // the legend: a project a colour
    let lx = 1
    const named = [...new Map(S.map(s => [s.project, s.color]))]
    for (const [i, [name, color]] of named.entries()) {
      const t = fit(name, 12), more = named.length - i - 1
      if (more && lx + t.length + 4 + 3 >= W) { // no room for the rest: how many more
        cv.write(lx, rows - 1, `+${more + 1}`, C.dim)
        break
      }
      if (lx + t.length + 2 >= W) {
        cv.write(lx, rows - 1, `+${more + 1}`, C.dim)
        break
      }
      cv.write(lx, rows - 1, '■', color)
      cv.write(lx + 2, rows - 1, t, C.muted)
      lx += t.length + 4
    }
    if (d.sessions.length > S.length) cv.write(W - 1 - `+${d.sessions.length - S.length}`.length, rows - 1, `+${d.sessions.length - S.length}`, C.dim)
    return null
  })
}
