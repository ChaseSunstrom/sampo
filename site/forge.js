/* T-2851: the illustrated forge's sky, flames and sparks, ported from 12-forge-illustrated; runs only when html has .anim */
const HB=[640, 415], RZ=[870, 84, 46];
(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const root = document.documentElement, ANIM = root.classList.contains('anim');
/* the forge rests an autoplay limit after the last input and wakes on the next; a hidden tab rests too */
let asleep = false, lastIn = performance.now();
const LIMIT = parseFloat(getComputedStyle(root).getPropertyValue('--autoplay-limit')) || 5000;
const LOOPS = ["drift", "breath", "steamw", "flick", "rune", "rvb", "rvg", "breathe", "burn", "cue", "emb", "blow", "fl", "barheat", "fly", "rivet", "lick", "sprise", "tw", "bank", "mk-burn", "mk-breathe", "mk-rune", "mk-rivet", "mk-hot", "mk-swing", "mk-flash", "mk-fly", "mk-rise", "mk-twinkle"];
function wake() {
  asleep = false; root.classList.remove('rest');
  document.getAnimations().forEach(a => LOOPS.includes(a.animationName) && a.playState === 'finished' && a.play());
  requestAnimationFrame(loop);
}
const touch = () => { lastIn = performance.now(); if (asleep) wake(); };
['pointermove', 'pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(t => addEventListener(t, touch, { passive: true }));
addEventListener('scroll', touch, { passive: true });
document.addEventListener('visibilitychange', () => { if (!document.hidden) touch(); });

const clamp = (v, a, b) => Math.max(a, Math.min(b, v)), lerp = (a, b, t) => a + (b - a) * t;
const mqDay = matchMedia('(prefers-color-scheme: light)');
let DAY = false, skyTouch = () => {}; const readDay = () => (DAY = root.dataset.theme === 'light' || (!root.dataset.theme && mqDay.matches));
readDay(); mqDay.addEventListener('change', readDay);

/* ---------- theme, copy, phone menu ---------- */
/* T-3996: site.js owns the switch and the menu on every page; the sky follows whatever sets the theme */
new MutationObserver(() => { readDay(); skyTouch(); }).observe(root, { attributes: true, attributeFilter: ['data-theme'] });

/* ---------- arrivals ---------- */
const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } }), { rootMargin: '0px 0px -10% 0px' });
$$('.rise, #log, #kinds, #ledger, #gauges, #chart').forEach(el => { if (!el.closest('.hero')) io.observe(el); });
requestAnimationFrame(() => $$('.hero .rise').forEach(el => el.classList.add('in')));
/* a station plays only while on screen, and is struck when it arrives */
const so = new IntersectionObserver(es => es.forEach(e => e.target.classList.toggle('on', e.isIntersecting)), { rootMargin: '120px 0px' });
const ho = new IntersectionObserver(es => es.forEach(e => {
  const s = e.target.closest('.sec'); if (!e.isIntersecting) return;
  s.classList.add('hit'); s.classList.remove('strike'); void s.offsetWidth; s.classList.add('strike');
}), { threshold: .3 });
$$('.sec, .foot').forEach(s => so.observe(s)); $$('.sec .side').forEach(s => ho.observe(s));

/* ---------- markup that waits in <noscript> (real DOM without script; raw text with it), set in one piece per task ---------- */
const srcs = $$('noscript.figsrc');
const secs = () => $$('.sec, .record, .foot');
const unskip = () => { const s = secs().find(x => !x.style.contentVisibility); if (!s) return; s.style.contentVisibility = 'visible'; setTimeout(unskip, 40); };
document.addEventListener('click', e => { const a = e.target.closest('a[href^="#"]'); if (a) secs().forEach(s => (s.style.contentVisibility = 'visible')); }, true);
const setFig = () => {
  const ns = srcs.shift(); if (!ns) { setTimeout(unskip, 60); return; }
  ns.insertAdjacentHTML('beforebegin', ns.textContent); const fig = ns.previousElementSibling; ns.remove();
  if (fig.classList.contains('smith')) wireSmith(fig);
  if (fig.classList.contains('how')) setSt(stIdx);
  setTimeout(setFig, 30);
};
/* the hero's still layers, far to near, one per task: the stage is built while the coals come up */
const lays = $$('.scene noscript.laysrc');
const setLay = () => {
  const ns = lays.shift(); if (!ns) { setTimeout(setFig, 40); return; }
  ns.insertAdjacentHTML('beforebegin', ns.textContent); const s = ns.previousElementSibling; ns.remove(); s.classList.add('arr');
  setTimeout(setLay, 24);
};
setTimeout(setLay, 0);

/* ---------- How: the camera goes to the station in view on the forge road ---------- */
const lis = $$('#stations li'), NUM = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII']; let stIdx = 0;
const setSt = i => {
  stIdx = i; lis.forEach((l, k) => l.classList.toggle('on', k === i));
  /* the camera travels down the road to the station (data-st), which lights (its .hs groups) and works once */
  const hf = $('.fig.how'); if (hf) { hf.dataset.st = i; hf.querySelectorAll('.hs').forEach(h => h.classList.toggle('on', +h.dataset.i === i)); }
  const stTag = $('#st-tag'); if (stTag) stTag.textContent = NUM[i] + ' · ' + lis[i].querySelector('h3').textContent;
};
let stTouch = 0;
/* the row being read: a thin line mid-screen on desktop; on phones a line two-thirds down, under the pinned road (thin, so one row at a time) */
const PHONE = matchMedia('(max-width: 900px)').matches, LINE = PHONE ? .66 : .49;
const lo = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { stTouch = performance.now(); setSt(lis.indexOf(e.target)); } }), { rootMargin: PHONE ? '-66% 0px -33% 0px' : '-49% 0px -50% 0px' });
lis.forEach(l => { lo.observe(l); l.addEventListener('pointerenter', () => { stTouch = performance.now(); setSt(lis.indexOf(l)); }); }); setSt(0);
/* before the list is read (row I still under the reading line), the camera walks the road once, a station every 2.6 s, and rests at
   the record house; once row I reaches the line, only the row being read moves it, so the lit station is always the row's */
if (ANIM) setInterval(() => { const h = lis[0] && lis[0].closest('.sec'); if (!document.hidden && !asleep && h && h.classList.contains('on') && stIdx < lis.length - 1
  && performance.now() - stTouch > 4000 && lis[0].getBoundingClientRect().top > innerHeight * LINE) setSt(stIdx + 1); }, 2600);

/* ---------- Smith: three lines are there from the start; each blow sets the next ones down and lights the one it ran ---------- */
const lines = $$('#log .ln'); let shown = 3, li = 0;
function wireSmith(fig) {
  fig.querySelector('.shm').addEventListener('animationiteration', () => {
    if (shown < lines.length) { for (let k = 0; k < 2 && shown < lines.length; k++) lines[shown++].classList.add('shown'); }
    const l = lines[Math.min(shown - 1, li++ % shown)]; l.classList.add('hot'); setTimeout(() => l.classList.remove('hot'), 650);
  });
}

/* ---------- Ward: put a command through again ---------- */
$$('#gate .cmd').forEach(b => b.addEventListener('click', () => {
  b.classList.remove('hit'); void b.offsetWidth; b.classList.add('hit');
  const wf = $('.fig.ward'); if (!wf) return;
  const ok = !!b.querySelector('.stamp.ok');
  wf.classList.remove('alarm', 'pass'); void wf.offsetWidth; wf.classList.add(ok ? 'pass' : 'alarm');
  clearTimeout(wf._t); wf._t = setTimeout(() => wf.classList.remove('alarm', 'pass'), 950);
}));

/* ---------- the headline is struck with the first blow ---------- */
const h1 = $('#h1');
if (ANIM) {
  const letters = [];
  const walk = n => [...n.childNodes].forEach(c => {
    if (c.nodeType === 3) {
      const frag = document.createDocumentFragment();
      c.textContent.split(/(\s+)/).forEach(w => {
        if (!w) return;
        if (/^\s+$/.test(w)) { frag.appendChild(document.createTextNode(w)); return; }
        const ws = document.createElement('span'); ws.className = 'w';
        for (const ch of w) { const l = document.createElement('span'); l.className = 'l'; l.textContent = ch; ws.appendChild(l); letters.push(l); }
        frag.appendChild(ws);
      });
      c.replaceWith(frag);
    } else walk(c);
  });
  walk(h1);
  letters.forEach((l, i) => l.style.setProperty('--d', ((letters.length - i) * 11) + 'ms'));
}

if (!ANIM) {
  const sk = $('#skysrc'); if (sk) sk.insertAdjacentHTML('afterend', sk.textContent);
  /* a single blow on request is not vestibular motion: the strike pose and a spark still for 300 ms, no loop */
  const sb = $('#strike-btn'), hm = $('#hammer'), sc = $('#scene');
  if (sb && hm) {
    sb.hidden = false; const raised = hm.style.transform;
    sb.addEventListener('click', () => { hm.style.transform = `translate(${(731.7 / HB[0] * 100).toFixed(3)}%,${(324.4 / HB[1] * 100).toFixed(3)}%) rotate(-12deg)`; sc.classList.add('struck'); setTimeout(() => { hm.style.transform = raised; sc.classList.remove('struck'); }, 300); });
  }
  return;
}

/* =================================================================== the forge, alive */
const hero = $('.hero'), scene = $('#scene'), fore = $('#fore'), glow = $('#glow'), bloom = $('#bloom'), carried = $('.scene .carry');
const layer = { far: $('.lay.far'), mid: $('.lay.mid'), fore, near: $('.lay.near') };
const STRIKE = [745, 387], BY = 398, F = v => v.toFixed(1);
let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;

/* ---- the fire, on its own canvas: the mark's layered tongues, drawn in scene units ---- */
const PL = [
  { x: 640, w: 70, h: 272, p: 0, core: 1 }, { x: 548, w: 46, h: 188, p: 1.7, core: 1 }, { x: 470, w: 28, h: 96, p: 3.1 },
  { x: 852, w: 42, h: 142, p: 4.2, core: 1 }, { x: 940, w: 26, h: 86, p: 2.4 }, { x: 1006, w: 18, h: 52, p: 5.3 },
  { x: 404, w: 17, h: 46, p: .9 }, { x: 716, w: 26, h: 104, p: 2.9 }, { x: 790, w: 18, h: 58, p: 3.7 }];
const GR = {           /* tip -> base, as the SVG gradients */
  red: [[0, 'rgba(255,120,40,0)'], [.3, 'rgba(255,120,40,0)'], [.55, 'rgba(250,110,36,.3)'], [.8, 'rgba(248,104,34,.62)'], [1, 'rgba(246,110,40,.85)']],     /* a glow round the flame's lower body: clear above 70 % of its height, so it never hangs over the sky as a brown shell */
  out: [[0, 'rgba(255,112,34,0)'], [.16, 'rgba(250,98,30,.6)'], [.3, 'rgba(246,104,32,.94)'], [.6, '#FF8A1F'], [1, '#FFC24D']],
  mid: [[0, 'rgba(255,176,64,0)'], [.35, '#FFC24D'], [1, '#FFF1C8']],
  core: [[0, 'rgba(61,107,255,0)'], [.4, '#4C78FF'], [.85, '#9FC0FF'], [1, '#EAF2FF']],
  grn: [[0, 'rgba(69,224,160,0)'], [.5, 'rgba(69,224,160,.8)'], [1, 'rgba(168,245,210,.9)']],
  vio: [[0, 'rgba(188,139,255,0)'], [.5, 'rgba(188,139,255,.8)'], [1, 'rgba(226,204,255,.9)']] };
const fcv = $('#firecv'), fc = fcv.getContext('2d');
let fk = 1, fdpr = 1;
/* flames are soft gradients: half the device resolution, upscaled by the compositor, costs a quarter of the raster */
function sizeFire() { fdpr = Math.min(devicePixelRatio || 1, 2) * .5; const w = fcv.clientWidth, h = fcv.clientHeight; fk = w / 800; fcv.width = Math.round(w * fdpr); fcv.height = Math.round(h * fdpr); }
const TL = [], TR = [];
function tonguePath(cx, by, w, h, t, ph, sw) {
  const N = 6; TL.length = 0; TR.length = 0;
  for (let i = 0; i <= N; i++) {
    const s = i / N, y = by - h * s, prof = i === N ? 0 : w * Math.pow(Math.sin(Math.PI * (.2 + .8 * s)), .8) * (1 - .3 * s);
    const d = sw * w * Math.pow(s, 1.4) * (.62 * Math.sin(s * 4.6 - t * 5.4 + ph) + .38 * Math.sin(s * 9.3 - t * 9.1 + ph * 1.9));
    TL.push(cx + d - prof, y); TR.push(cx + d + prof, y);
  }
  fc.beginPath(); fc.moveTo(TR[0], TR[1]); fc.quadraticCurveTo(cx, by + w * .5, TL[0], TL[1]);
  cr(TL, false); cr(TR, true); fc.closePath();
}
function cr(P, rev) {    /* Catmull-Rom through the points (reversed for the right edge), as cubic segments */
  const n = P.length / 2, X = i => P[2 * (rev ? n - 1 - i : i)], Y = i => P[2 * (rev ? n - 1 - i : i) + 1];
  for (let i = 0; i < n - 1; i++) {
    const a = Math.max(0, i - 1), d = Math.min(n - 1, i + 2);
    fc.bezierCurveTo(X(i) + (X(i + 1) - X(a)) / 6, Y(i) + (Y(i + 1) - Y(a)) / 6, X(i + 1) - (X(d) - X(i)) / 6, Y(i + 1) - (Y(d) - Y(i)) / 6, X(i + 1), Y(i + 1));
  }
}
function fillG(k, top, bot, alpha) {
  const g = fc.createLinearGradient(0, top, 0, bot); for (const [o, c] of GR[k]) g.addColorStop(o, c);
  fc.globalAlpha = alpha; fc.fillStyle = g; fc.fill(); fc.globalAlpha = 1;
}
function sheetPath(t, hgt, k0) {
  fc.beginPath(); fc.moveTo(372, BY + 3);      /* as art.fire's sheet: the stills freeze this at t = .35 */
  for (let i = 0; i <= 26; i++) {
    const x = 372 + i * (668 / 26), env = Math.exp(-Math.pow((x - 706) / 300.6, 2));
    fc.lineTo(x, BY - hgt * (.35 + .65 * env) * (.75 + .25 * Math.sin(x * .045 - t * 3.1 + k0) + .18 * Math.sin(x * .11 + t * 4.7 + k0 * 2)));
  }
  fc.lineTo(1040, BY + 3); fc.closePath();
}
const beds = Array.from({ length: 9 }, (_, i) => ({ x: 400 + i * 78 + rnd() * 30, p: rnd() * 9, f: 1.5 + rnd() * 2.5 }));
const smoke = Array.from({ length: 7 }, (_, i) => ({ x: 520 + rnd() * 260, p: rnd() * 30, s: .6 + rnd() * .5, w: 26 + rnd() * 30 }));
let coalsUp = 0;
function drawFire(t, heat, ig, puff, flare) {
  fc.setTransform(1, 0, 0, 1, 0, 0); fc.clearRect(0, 0, fcv.width, fcv.height);
  fc.setTransform(fdpr * fk, 0, 0, fdpr * fk, -300 * fdpr * fk, 0);
  const redA = DAY ? .12 : .62;
  /* the bed breathes in soft pools, and each puff from the bellows runs through it left to right as a bright wave */
  fc.globalCompositeOperation = 'lighter';
  for (const c of beds) {
    const wave = puff.a * Math.max(0, 1 - Math.abs(c.x - puff.x) / 110) + ptr.a * .5 * Math.max(0, 1 - Math.abs(c.x - ptr.x) / 120);
    const a = (ig * .5 + (1 - ig) * coalsUp * .5) * (.25 + .2 * Math.sin(t * c.f + c.p)) + wave * .8 + flare * .12;
    if (a < .02) continue;
    fc.save(); fc.translate(c.x, 384); fc.scale(1, .3);
    const g = fc.createRadialGradient(0, 0, 0, 0, 0, 70); g.addColorStop(0, `rgba(255,${wave > .3 ? 236 : 176},${wave > .3 ? 190 : 90},${Math.min(1, a).toFixed(3)})`); g.addColorStop(1, 'rgba(255,90,20,0)');
    fc.fillStyle = g; fc.fillRect(-70, -70, 140, 140); fc.restore();
  }
  fc.globalCompositeOperation = 'source-over';
  if (ig < .01) return;
  sheetPath(t * .8, 74 * heat, 1.3); fillG('red', BY - 74 * heat, 404, redA);
  sheetPath(t, 52 * heat, 0); fillG('out', BY - 52 * heat, 404, 1);
  const H = [];
  PL.forEach((pl, i) => {
    const lean = puff.a * 12 * Math.max(0, 1 - Math.abs(pl.x - puff.x) / 260) + ptr.a * 16 * Math.sign(pl.x - ptr.x) * Math.max(0, 1 - Math.abs(pl.x - ptr.x) / 200);   /* away from the puff, and from a pointer near the fire */
    H[i] = pl.h * heat * (1 + .08 * Math.sin(t * 7.3 + pl.p) + .05 * Math.sin(t * 12.9 + pl.p * 2.1) + .04 * Math.sin(t * 2.7 + pl.p));
    pl.lean = lean; pl.ww = pl.w * (.92 + .08 * heat);
  });
  for (const [k, fn] of [['red', 0], ['out', 1], ['out2', 2], ['grn', 3], ['vio', 4], ['mid', 5], ['core', 6]]) {
    PL.forEach((pl, i) => {
      const hh = H[i], w = pl.ww, x = pl.x;
      if (hh < 2) return;
      if ((k === 'out2' || k === 'grn' || k === 'vio') && pl.w < 12) return;
      if (k === 'core' && !pl.core) return;
      const sw = 1 + Math.abs(pl.lean) * .03;
      switch (fn) {
        case 0: tonguePath(x, BY + 4, w * 1.32, hh * 1.12, t * .8, pl.p + .5, .7 * sw); fillG('red', BY - hh * 1.12, BY + 4, redA); break;
        case 1: tonguePath(x + pl.lean * .6, BY, w, hh, t, pl.p, .9 * sw); fillG('out', BY - hh, BY, 1); break;
        case 2: tonguePath(x - w * .32, BY, w * .55, hh * .68, t * 1.15, pl.p + 2.2, 1.3); fillG('out', BY - hh * .68, BY, 1); break;
        case 3: tonguePath(x + w * .44, BY - hh * .06, w * .28, hh * .5, t * 1.3, pl.p + 1, .9); fillG('grn', BY - hh * .56, BY - hh * .06, 1); break;
        case 4: tonguePath(x - w * .46, BY - hh * .05, w * .26, hh * .46, t * 1.25, pl.p + 4, .9); fillG('vio', BY - hh * .51, BY - hh * .05, 1); break;
        case 5: tonguePath(x, BY, w * .62, hh * .74, t * 1.05, pl.p + .3, .8); fillG('mid', BY - hh * .74, BY, 1); break;
        case 6: tonguePath(x, BY, w * .32, hh * .44, t * 1.2, pl.p + .6, .5); fillG('core', BY - hh * .44, BY, 1);
          fc.globalAlpha = .5 + .2 * Math.sin(t * 9 + pl.p) + .2 * ig; fc.fillStyle = '#F4F8FF';
          fc.beginPath(); fc.moveTo(x - w * .5, BY - 1.5); fc.lineTo(x, BY - w * .16 - 1.5); fc.lineTo(x + w * .5, BY - 1.5); fc.lineTo(x, BY + w * .16 - 1.5); fc.closePath(); fc.fill(); fc.globalAlpha = 1; break;
      }
    });
  }
  /* embers riding the convection, drawn with the fire */
  fc.globalCompositeOperation = 'lighter';
  for (const p of P) {
    if (p.kind !== 1) continue;
    const c = heatRGB(p.T, p.salt), a = clamp(p.T * 2.4, 0, 1) * Math.min(1, p.life * 2) * Math.min(1, (p.max - p.life) * 1.5), r = p.w * (.7 + .3 * Math.sin(p.life * 9 + p.ph));
    fc.fillStyle = css(c, a * .22); fc.fillRect(p.x - r * 1.3, p.y - r * 1.3, r * 2.6, r * 2.6);     /* a point and its glow, never a streak */
    fc.fillStyle = css(c, a); fc.fillRect(p.x - r * .5, p.y - r * .5, r, r);
  }
  fc.globalCompositeOperation = 'source-over';
  /* smoke drifting right with its underside lit */
  for (const s of smoke) {
    const life = ((t * s.s * .08 + s.p / 30) % 1), y = 214 - life * 140,     /* kept clear of the canvas's top edge */ x = s.x + life * 140 + Math.sin(t * .6 + s.p) * 12, a = Math.sin(Math.PI * life) * (DAY ? .16 : .13) * ig, R = s.w * (1.4 + life);
    fc.save(); fc.translate(x, y); fc.scale(1, .42);
    const g = fc.createRadialGradient(0, R * .35, 0, 0, 0, R); g.addColorStop(0, DAY ? `rgba(170,140,130,${a.toFixed(3)})` : `rgba(150,96,70,${a.toFixed(3)})`); g.addColorStop(.5, DAY ? `rgba(150,146,162,${(a * .7).toFixed(3)})` : `rgba(84,74,92,${(a * .7).toFixed(3)})`); g.addColorStop(1, DAY ? 'rgba(150,146,162,0)' : 'rgba(84,74,92,0)');
    fc.fillStyle = g; fc.fillRect(-R, -R, 2 * R, 2 * R); fc.restore();
  }
}

/* ---- bellows: pear-shaped boards seen from above at an angle, hinged at the neck; the pleated belly swells as they open
   and sags as they shut; each breath glows in the nozzle's mouth and runs through the bed. Mirrors hero.bellows_paths ---- */
const BW_HALF = [[0, 10], [-15, 13], [-35, 22], [-60, 34], [-85, 42], [-105, 44], [-122, 40], [-134, 30], [-140, 15], [-142, 0]], BW_K = .38, BW_G = 6, BW_N = 5;
const bwTop = $('#bwTop'), bwLight = $('#bwLight'), bwDark = $('#bwDark'), bwFold = $('#bwFold'), bwMouth = $('#bwMouth');
function bellows(b, mouth) {
  const tn = Math.tan((6 + 10 * b) * Math.PI / 180), bul = .08 + .22 * b;
  const ring = k => { const e = bul * Math.sin(Math.PI * k); return BW_HALF.map(([u, w]) => [u * (1 + e * .25), w * (1 + e) * BW_K - k * (BW_G - u * tn)]); };
  const R = []; for (let i = 0; i <= BW_N; i++) R.push(ring(i / BW_N));
  const poly = P => 'M' + P.map(p => F(p[0]) + ' ' + F(p[1])).join('L') + 'Z';
  let L = '', D = '', fo = '';
  for (let i = 0; i < BW_N; i++) { const q = poly(R[i].concat(R[i + 1].slice().reverse())); if (i % 2) D += q; else L += q; }
  for (let i = 1; i < BW_N; i++) fo += 'M' + R[i].map(p => F(p[0]) + ' ' + F(p[1])).join('L');
  bwLight.setAttribute('d', L); bwDark.setAttribute('d', D); bwFold.setAttribute('d', fo);
  bwTop.setAttribute('transform', `matrix(1 ${tn.toFixed(4)} 0 1 0 ${-BW_G})`);
  bwMouth.style.opacity = mouth.toFixed(3);
}

/* ---- the hammer: raised, wound up, the blow, the rebound, lifted again; side-on, the handle rising to the smith ---- */
const hm = $('#hammer'), PER = 3.4, FIRST = 1.6;
/* the blow, the raised pose (shared with the stills), and the lift before each blow: the head rises and the wrist cocks back */
const I = [731.7, 324.4, -12], Rz = RZ, R2 = [RZ[0] + 14, RZ[1] - 34, RZ[2] + 8], OFF = [1420, -420, 64];   /* off the frame on every width (phones show the scene 120 % wide) */
const bez = (a, c, b, t) => [lerp(lerp(a[0], c[0], t), lerp(c[0], b[0], t), t), lerp(lerp(a[1], c[1], t), lerp(c[1], b[1], t), t)];
const eio = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2, eout = t => 1 - Math.pow(1 - t, 3);
function pose(t) {
  const tau = ((t - FIRST) % PER + PER) % PER;
  let p;
  if (tau < .46) { const d = tau; p = [I[0], I[1] - 20 * Math.exp(-6 * d) * Math.abs(Math.sin(d * 15)), I[2] - 3 * Math.exp(-6 * d) * Math.sin(d * 15)]; }
  else if (tau < 1.42) { const u = eio((tau - .46) / .96), q = bez(I, [770, 170], Rz, u); p = [q[0], q[1], lerp(I[2], Rz[2], u)]; }
  else if (tau < PER - .62) { const u = tau - 1.42; p = [Rz[0] + Math.sin(u * 1.9) * 3, Rz[1] + Math.sin(u * 2.6) * 4, Rz[2] + Math.sin(u * 1.9) * .8]; }
  else if (tau < PER - .16) { const u = eout((tau - (PER - .62)) / .46); p = [lerp(Rz[0], R2[0], u), lerp(Rz[1], R2[1], u), lerp(Rz[2], R2[2], u)]; }
  else { const u = Math.pow((tau - (PER - .16)) / .16, 2.4), q = bez(R2, [800, 100], I, u); p = [q[0], q[1], lerp(R2[2], I[2], Math.pow(u, .8))]; }
  if (t < 1.45) { const u = eout(clamp((t - .85) / .6, 0, 1)); p = [lerp(OFF[0], p[0], u), lerp(OFF[1], p[1], u), lerp(OFF[2], p[2], u)]; }
  return { p, tau, down: tau >= PER - .16 };
}
const tfOf = p => `translate(${(p[0] / HB[0] * 100).toFixed(3)}%,${(p[1] / HB[1] * 100).toFixed(3)}%) rotate(${p[2].toFixed(2)}deg)`, setHammer = p => (hm.style.transform = tfOf(p));
const HEAD = [[-24, 64], [24, 64], [27, 58], [27, -26], [9, -86], [-9, -86], [-27, -26], [-27, 58]];

/* ---- sparks, scale and embers: one canvas over the scene, drawn in scene units ---- */
const fx = $('#fx'), cx = fx.getContext('2d');
let k = 1, dpr = 1;
/* a canvas's backing store is capped near 2.6 million pixels: past that a software rasteriser spends the frame compositing it */
const capDpr = (w, h) => Math.max(1, Math.min(devicePixelRatio || 1, 2, Math.sqrt(2.6e6 / (w * h))));
/* the spark canvas covers only where sparks, flakes, the smear and the flash can be (scene x 340-1180, y 40-960), at 1.25x at most:
   a second full-size 2x canvas changing every frame was what cost the frame at 1440@2x */
const FXB = [340, -160, 840, 1120];
function sizeFx() { const w = scene.clientWidth; dpr = Math.min(devicePixelRatio || 1, 1.25); k = w / 1200; fx.width = Math.round(FXB[2] * k * dpr); fx.height = Math.round(FXB[3] * k * dpr); }
const HOT = [[120, 30, 16], [226, 72, 26], [255, 148, 64], [255, 194, 77], [255, 240, 200], [234, 242, 255]];
const SALT = [null, [69, 224, 160], [188, 139, 255], [110, 155, 255]];
function heatRGB(T, salt) {
  const x = clamp(T, 0, 1) * 5, i = Math.min(4, Math.floor(x)), f = x - i, a = HOT[i], b = HOT[i + 1];
  let c = [lerp(a[0], b[0], f), lerp(a[1], b[1], f), lerp(a[2], b[2], f)];
  if (salt && T < .85) { const s = SALT[salt], m = Math.min(1, (.85 - T) * 3) * (T > .15 ? 1 : T / .15); c = [lerp(c[0], s[0], m), lerp(c[1], s[1], m), lerp(c[2], s[2], m)]; }
  return c;
}
const css = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a.toFixed(3)})`;
const P = []; let flashT = 9, flare = 0, shakeT = 9, strikes = 0;
/* kinds: 0 spark, 1 ember (rises, may reach the sky), 2 scale cooling on the floor, 3 flake of mill scale off the billet */
function burst() {
  for (let i = 0; i < 60; i++) {
    const climb = rnd() < .14, a = climb ? -Math.PI / 2 + (rnd() - .5) * .9 : -Math.PI / 2 + (rnd() - .5) * Math.PI * 1.08;
    const v = climb ? 380 + rnd() * 260 : 260 + Math.pow(rnd(), 1.4) * 820;
    P.push({ x: STRIKE[0] + (rnd() - .5) * 30, y: STRIKE[1] - 2, vx: Math.cos(a) * v, vy: Math.sin(a) * v, g: climb ? 90 : 1150, life: 0, max: climb ? 6 : .5 + rnd() * .9,
             w: .9 + rnd() * 1.5, salt: rnd() < .15 ? 1 + Math.floor(rnd() * 3) : 0, T: 1, cool: climb ? .14 : .7 + rnd() * .5, kind: climb ? 1 : 0, ph: rnd() * 6 });
  }
  for (let i = 0; i < 10; i++) {      /* scale bursting off the bar */
    const side = rnd() < .5 ? -1 : 1;     /* thrown clear of the bar, so they land on the face beside it, never on its hot length */
    P.push({ x: lerp(660, 760, rnd()), y: 386, vx: side * (230 + rnd() * 160), vy: -(110 + rnd() * 90), g: 900, life: 0, max: 3, w: 2 + rnd() * 2, T: .35, cool: .25, kind: 3, ph: rnd() * 6, rot: rnd() * 6 });
  }
}
function ember() {
  P.push({ x: 420 + rnd() * 600, y: BY - 20 - rnd() * 30, vx: (rnd() - .5) * 20, vy: -40 - rnd() * 60, g: -26, life: 0, max: 5 + rnd() * 4, w: .9 + rnd() * 1.4,
           salt: rnd() < .14 ? 1 + Math.floor(rnd() * 3) : 0, T: .55 + rnd() * .3, cool: .07, kind: 1, ph: rnd() * 6 });
}
let sceneRect = { left: 0, top: 0 }, foreY = 0, sceneScale = 1;
function stepFx(dt) {
  for (let i = P.length - 1; i >= 0; i--) {
    const p = P[i]; p.life += dt;
    if (p.kind === 2) { p.T = Math.max(0, p.T - dt / 3); if (p.T <= 0) P.splice(i, 1); continue; }
    if (p.kind === 1) { p.vx += Math.sin(p.life * 2.6 + p.ph) * 28 * dt; p.vx *= 1 - .6 * dt; p.vy *= 1 - .25 * dt; }
    else { p.vx *= 1 - 1.1 * dt; p.vy *= 1 - .6 * dt; }
    const py = p.y;
    p.vy += p.g * dt; p.x += p.vx * dt; p.y += p.vy * dt; p.T = Math.max(0, p.T - p.cool * dt);
    if (p.kind === 3) {                                   /* flakes land on the face and lie there, cooling */
      if (p.vy > 0 && p.y >= 404 && (p.x < 645 || p.x > 775)) { p.y = 404; p.vx *= .3; p.vy = 0; p.g = 0; }   /* never on the bar itself */
      if (p.life > p.max) P.splice(i, 1); continue;
    }
    if (p.kind === 0) {
      if (p.vy > 0 && py < 404 && p.y >= 404 && p.x > 470 && p.x < 1000) { p.y = 404; p.vy *= -.32; p.vx *= .7; }      /* off the anvil's face */
      else if (p.vy > 0 && py < 398 && p.y >= 398 && p.x > 340 && p.x < 1062 && (p.x < 470 || p.x > 1000)) { P.splice(i, 1); continue; }  /* into the coals */
      else if (p.vy > 0 && p.y > 900) {
        if (p.vy < 170) { P[i] = { kind: 2, x: p.x, y: 900 + rnd() * 30, T: .55, salt: p.salt, w: p.w }; continue; }       /* lies on the floor as scale */
        p.y = 900; p.vy *= -.3; p.vx *= .55;
      }
    }
    /* an ember that climbed out of the fire's frame, or stopped in the cold air above it, settles as a star */
    if (p.kind === 1 && (p.y < 2 || (p.y < 220 && (p.T < .12 || Math.hypot(p.vx, p.vy) < 8)))) { Sky.bear(sceneRect.left + p.x * k * sceneScale + (Math.random() - .5) * 400, sceneRect.top + p.y * k * sceneScale + foreY + (Math.random() - .5) * 240, p.salt); P.splice(i, 1); continue; }
    if (p.life > p.max || (p.kind === 1 && p.T <= 0)) P.splice(i, 1);
  }
}
let ghosts = [], fxDirty = false;
/* only the region the sparks, flakes, smear and flash cover is cleared and redrawn, so the canvas damages only that much */
let fxBox = null;
function bounds(hp) {
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9; const add = (x, y, m) => { x0 = Math.min(x0, x - m); y0 = Math.min(y0, y - m); x1 = Math.max(x1, x + m); y1 = Math.max(y1, y + m); };
  for (const p of P) if (p.kind !== 1) add(p.x, p.y, 4 + Math.abs(p.vx || 0) * .07 + Math.abs(p.vy || 0) * .07);
  if (flashT < .5) { add(STRIKE[0] - 520, STRIKE[1], 6); add(STRIKE[0] + 520, STRIKE[1], 6); add(STRIKE[0], STRIKE[1], 290); }
  if (hp.down) add(hp.p[0], hp.p[1], 110);
  for (const g of ghosts) add(g[0], g[1], 110);          /* the smear's earlier heads too, or they would be left behind */
  return x1 < x0 ? null : [x0, y0, x1, y1];
}
function drawFx(hp) {
  const nb = bounds(hp), ob = fxBox, u = [nb, ob].filter(Boolean);
  fxBox = nb; if (!hp.down) ghosts = [];
  if (!u.length) return;
  const bx = [Math.min(...u.map(b => b[0])), Math.min(...u.map(b => b[1])), Math.max(...u.map(b => b[2])), Math.max(...u.map(b => b[3]))];
  cx.setTransform(dpr * k, 0, 0, dpr * k, -FXB[0] * dpr * k, -FXB[1] * dpr * k);
  cx.clearRect(bx[0] - 2, bx[1] - 2, bx[2] - bx[0] + 4, bx[3] - bx[1] + 4);
  /* the hammer's smear on the downswing: its head, three frames behind */
  if (hp.down) ghosts.push(hp.p);
  ghosts.slice(-4, -1).forEach((g, i) => {
    cx.save(); cx.translate(g[0], g[1]); cx.rotate(g[2] * Math.PI / 180); cx.fillStyle = `rgba(190,170,160,${(.07 + i * .05).toFixed(3)})`;
    cx.beginPath(); HEAD.forEach(([x, y], j) => (j ? cx.lineTo(x, y) : cx.moveTo(x, y))); cx.closePath(); cx.fill(); cx.restore();
  });
  /* scale and flakes, drawn plain */
  for (const p of P) {
    if (p.kind === 2) { const c = heatRGB(p.T, p.salt); cx.fillStyle = css(c, .4 + p.T); cx.fillRect(p.x - p.w, p.y - .6, p.w * 2, 1.2); }
    else if (p.kind === 3) { const a = clamp((p.max - p.life) / 1.2, 0, 1); cx.fillStyle = p.T > .1 ? css(heatRGB(p.T + .1, 0), a) : `rgba(40,36,40,${a.toFixed(3)})`; cx.save(); cx.translate(p.x, p.y); cx.rotate(p.rot + p.life * 4 * (p.g ? 1 : 0)); cx.fillRect(-p.w / 2, -p.w / 3, p.w, p.w * .66); cx.restore(); }
  }
  cx.globalCompositeOperation = 'lighter';
  if (flashT < .5) {
    const u = flashT / .5, a = Math.pow(1 - u, 2), [x, y] = STRIKE;
    const g = cx.createRadialGradient(x, y, 0, x, y, 170 + 90 * u);
    g.addColorStop(0, `rgba(235,242,255,${(.95 * a).toFixed(3)})`); g.addColorStop(.18, `rgba(150,185,255,${(.55 * a).toFixed(3)})`); g.addColorStop(.5, `rgba(255,150,60,${(.22 * a).toFixed(3)})`); g.addColorStop(1, 'rgba(255,90,20,0)');
    cx.fillStyle = g; cx.fillRect(x - 280, y - 280, 560, 560);
    const r = 34 + 50 * u; cx.fillStyle = `rgba(244,248,255,${(a * .95).toFixed(3)})`;
    cx.beginPath(); cx.moveTo(x, y - r); cx.lineTo(x + r * .14, y - r * .14); cx.lineTo(x + r, y); cx.lineTo(x + r * .14, y + r * .14); cx.lineTo(x, y + r); cx.lineTo(x - r * .14, y + r * .14); cx.lineTo(x - r, y); cx.lineTo(x - r * .14, y - r * .14); cx.closePath(); cx.fill();
    const sg = cx.createLinearGradient(x - 520, 0, x + 520, 0); sg.addColorStop(0, 'rgba(140,180,255,0)'); sg.addColorStop(.5, `rgba(220,232,255,${(.8 * a).toFixed(3)})`); sg.addColorStop(1, 'rgba(140,180,255,0)');
    cx.fillStyle = sg; cx.fillRect(x - 520, y - 1.6, 1040, 3.2);
  }
  cx.lineCap = 'round';
  for (const p of P) {
    if (p.kind !== 0) continue;
    const c = heatRGB(p.T, p.salt), a = clamp(p.T * 2.4, 0, 1);
    /* a spark: a long faint tail tapering into a bright head */
    const tl = .02 + .03 * p.T, hl = .006 + .01 * p.T;
    cx.strokeStyle = css(c, a * .18); cx.lineWidth = p.w * .35; cx.beginPath(); cx.moveTo(p.x - p.vx * tl * 2.4, p.y - p.vy * tl * 2.4); cx.lineTo(p.x, p.y); cx.stroke();
    cx.strokeStyle = css(c, a * .45); cx.lineWidth = p.w * (.5 + p.T * .4); cx.beginPath(); cx.moveTo(p.x - p.vx * tl, p.y - p.vy * tl); cx.lineTo(p.x, p.y); cx.stroke();
    cx.strokeStyle = css(c, a); cx.lineWidth = p.w * (.7 + p.T * .8); cx.beginPath(); cx.moveTo(p.x - p.vx * hl, p.y - p.vy * hl); cx.lineTo(p.x, p.y); cx.stroke();
  }
  cx.globalCompositeOperation = 'source-over';
}

/* ---- what a blow does to the rest of the scene ---- */
let rgs = [], rgc = [], rvB = [], rvG = [], barhot = null, billet = null, kindled = false, squash = 0;
const RUNE_X = [57, 100, 128, 150, 184].map(x => 3.2 * x + 290.4);
const crawl = (i, delay) => rgc[i] && rgc[i].animate([{ strokeDashoffset: 100, opacity: 0 }, { opacity: .95, offset: .12 }, { opacity: .95, offset: .85 }, { strokeDashoffset: 0, opacity: 0 }], { duration: 1500, delay, easing: 'ease-in-out' });
let bflare = null, barglow = null, carry = null, reheating = false;
function grab() { rgs = $$('.scene .rg'); rgc = $$('.scene .rgc .crawl'); rvB = $$('.scene .rv.b'); rvG = $$('.scene .rv.g'); billet = $('#billet'); barhot = billet && billet.querySelector('.barhot'); bflare = billet && billet.querySelector('.bflare'); barglow = billet && billet.querySelector('.barglow'); carry = $('.scene .carry'); $$('.scene .rv').forEach(e => (e.style.animation = 'none')); }
function kindle() { /* on arrival the five runes take fire in turn, left to right */
  grab(); if (!rgs.length) return; kindled = true;
  rgs.forEach((e, i) => { e.animate([{ opacity: 0 }, { opacity: 1, offset: .3 }, { opacity: .2 }], { duration: 1300, delay: i * 140, easing: 'ease-out' }); crawl(i, i * 140); });
  rvB.forEach((e, i) => e.animate([{ opacity: 0 }, { opacity: .9, offset: .2 }, { opacity: 0 }], { duration: 700, delay: 200 + i * 70, easing: 'ease-out' }));
}
/* every fifth blow, once the hammer has lifted clear (0.6 s), the tongs carry the bar back into the fire, hold it in the
   coals, and bring it out white-yellow before the next lift (2.1 s; a click can't call the blow early meanwhile). The bar
   and the tongs share one wrapper (.carry), so they move as one: 1.5 s of motion every 17 s, and none at rest */
function reheat() {
  if (!carry) return; reheating = true;
  const k = scene.clientWidth / 1200, inFire = `translate(${(-38 * k).toFixed(1)}px,${(-24 * k).toFixed(1)}px)`, ease = 'cubic-bezier(.45,0,.4,1)';
  carry.animate([{ transform: 'none', easing: ease }, { transform: inFire, offset: .3 }, { transform: inFire, offset: .7, easing: ease }, { transform: 'none' }], { duration: 1500, delay: 600 });
  if (barglow) barglow.animate([{ opacity: .56 }, { opacity: .2, offset: .3 }, { opacity: .2, offset: .7 }, { opacity: 1 }], { duration: 1500, delay: 600, fill: 'forwards' });
  setTimeout(() => { flare = Math.max(flare, .7); squash = 0; barhot.animate([{ opacity: .32 }, { opacity: 1 }], { duration: 600, easing: 'ease-in', fill: 'forwards' }); }, 1050);   /* in the coals: the fire flares, the bar heats and loses its flattening */
  setTimeout(() => (reheating = false), 2100);
}
function strike() {
  if (!barhot) grab(); if (!barhot) return;
  strikes++; flashT = 0; flare = 1; shakeT = 0; burst();
  rgs.forEach((r, i) => r.animate([{ opacity: 0 }, { opacity: 1, offset: .12 }, { opacity: 0 }], { duration: 1500, delay: Math.abs(RUNE_X[i] - STRIKE[0]) * 1.1, easing: 'ease-out' }));
  crawl(strikes % 5, 120);
  const j = (strikes - 1) % 9;
  rvB[j].animate([{ opacity: 1 }, { opacity: 0 }], { duration: 1000, easing: 'ease-out' });
  rvG[j].animate([{ opacity: 0 }, { opacity: .9, offset: .25 }, { opacity: 0 }], { duration: 2800, delay: 250, easing: 'ease-out' });
  /* the billet flattens a little with each blow; every fifth it goes back to the fire and comes out fresh */
  squash = strikes % 5;
  /* the fresh heat fades blow by blow, but through colour: under it the bar stays orange-hot, and its light never goes out */
  const cool = { duration: 2800, easing: 'cubic-bezier(.2,.6,.3,1)', fill: 'forwards' };
  barhot.animate([{ opacity: 1 }, { opacity: squash ? 1 - .17 * squash : .9 }], cool);
  if (barglow) barglow.animate([{ opacity: 1 }, { opacity: Math.max(.55, 1 - .11 * squash) }], cool);
  if (squash === 4) reheat();
  /* the bloom is a screen-blended layer over the whole scene: shown only for its 160 ms, since even at opacity 0 it costs a blend pass every frame */
  bloom.style.visibility = 'visible'; bloom.animate([{ opacity: 0 }, { opacity: .55, offset: .25 }, { opacity: 0 }], { duration: 160, easing: 'ease-out' }).onfinish = () => (bloom.style.visibility = '');
  if (bflare) bflare.animate([{ opacity: .9 }, { opacity: 0 }], { duration: 260, easing: 'ease-out' });      /* the bar flares white where it was struck */
  if (strikes === 1) h1.classList.add('go');
}

/* ---- the clock ---- */
let heroOn = true, last = 0, t0 = 0, prevTau = 9, emberAcc = 0, tShift = 0, frameN = 0, sq = 0, arrived = false;
/* a pointer near the fire bends the plumes away; over the stone it kindles the nearest rune */
const ptr = { x: 0, a: 0, on: false }, lastKindle = [0, 0, 0, 0, 0];
scene.addEventListener('pointermove', e => {
  const r = scene.getBoundingClientRect(), u = (e.clientX - r.left) / r.width * 1200, v = (e.clientY - r.top) / r.height * 1000;
  ptr.x = u; ptr.on = v > 80 && v < 430 && u > 360 && u < 1060;
  if (v > 640 && v < 820 && rgs.length) {
    const i = RUNE_X.reduce((b, x, j) => Math.abs(x - u) < Math.abs(RUNE_X[b] - u) ? j : b, 0), now = performance.now();
    if (Math.abs(RUNE_X[i] - u) < 60 && now - lastKindle[i] > 900) { lastKindle[i] = now; rgs[i].animate([{ opacity: 0 }, { opacity: .9, offset: .25 }, { opacity: 0 }], { duration: 1100, easing: 'ease-out' }); crawl(i, 0); }
  }
});
scene.addEventListener('pointerleave', () => (ptr.on = false));
const callBlow = () => { if (!reheating && prevTau > 1.42 && prevTau < PER - .62) tShift += (PER - .62) - prevTau; };
scene.addEventListener('click', callBlow); const sb = $('#strike-btn'); if (sb) { sb.hidden = false; sb.addEventListener('click', callBlow); }
new IntersectionObserver(es => (heroOn = es[0].isIntersecting)).observe(hero);
const lite = { pool: null, anv: null, shad: null, rim: null };
let hzT = 0, hzK = 0;
function frameHero(t, dt) {
  /* the arrival: the coals come up first, the fire catches at 0.6 s, the anvil is lit in, the hammer swings in, the first blow at 1.6 s */
  const ig = t < .6 ? 0 : 1 - Math.exp(-5 * (t - .6)) * Math.cos(6 * (t - .6));
  coalsUp = clamp(t / .6, 0, 1);
  const odd = frameN % 2 === 1, b = .5 - .5 * Math.cos(t * 2 * Math.PI / 4.2);
  flare = Math.max(0, flare - dt * 1.6);
  /* the breath: as the bellows shut, a puff runs through the bed left to right over 400 ms; the fire answers it */
  const ct = t % 4.2, pu = (ct - 2.75) / .4, pa = pu < 0 ? 0 : pu < 1 ? 1 : Math.max(0, 1 - (pu - 1) / 1.2);
  const puff = { x: 380 + clamp(pu, 0, 1.4) * 660, a: pa * coalsUp };
  const heat = (1 + puff.a * .14 + flare * .22) * ig;
  const flick = .5 + .3 * Math.sin(t * 11.3) + .2 * Math.sin(t * 17.9 + 1);
  ptr.a = lerp(ptr.a, ptr.on ? 1 : 0, 1 - Math.exp(-dt * 6));
  /* the heat haze: the air over the fire bends the hammer; three still noises, a new one every 140 ms (the layer re-rasters only then) */
  if (ig > .5 && t - hzT > .14) { hzT = t; hm.dataset.hz = hzK = (hzK + 1) % 3; }
  if (odd) { drawFire(t, heat, Math.max(ig, 0), puff, flare); bellows(b, coalsUp * (.3 + .7 * (pu > -.2 && pu < 1.2 ? 1 - Math.abs(pu - .3) / .9 : 0))); }
  const hp = pose(t + tShift); setHammer(hp.p);
  if (hp.tau < prevTau && t > FIRST - .05) strike();
  if (!kindled && t > 1.0) kindle();
  prevTau = hp.tau;
  emberAcc += dt * 6 * ig; while (emberAcc > 1) { emberAcc--; ember(); }
  flashT += dt; shakeT += dt;
  stepFx(dt);
  const busy = flashT < .6 || hp.down || P.some(p => p.kind === 0), slow = P.some(p => p.kind >= 2);
  if (odd && (busy || (slow && frameN % 6 === 1) || (fxDirty && !slow))) { drawFx(hp); fxDirty = busy || slow; }   /* with the fire, on the odd frames */
  /* the billet and its tongs (one wrapper) come in with the anvil, 0.9-1.5 s, never before the hearth; then the billet eases toward its flattened shape */
  if (!arrived && carried) { carried.style.opacity = clamp((t - .9) / .6, 0, 1).toFixed(2); arrived = t > 1.55; }
  sq = lerp(sq, squash, 1 - Math.exp(-dt * 14));
  if (billet) billet.style.transform = `scale(${(1 + .025 * sq).toFixed(4)},${(1 - .07 * sq).toFixed(4)})`;
  /* one light: the fire's flicker and breath, and the blow's flash, on everything the fire reaches */
  const fl = Math.max(0, 1 - flashT * 6), L = ig * (.72 + .12 * flick + puff.a * .1) + fl * .5;
  if (frameN % 4 === 0 || fl > 0) {      /* the light rig at 15 Hz (every frame through a flash): each write recomposites its layer */
    q(glow, Math.max(coalsUp * .35, ig * (.76 + puff.a * .1) + fl * .5));
    if (!lite.pool) { lite.pool = $('.scene .lite.pool'); lite.anv = $('.scene .lite.anv'); lite.shad = $('.scene .shad'); lite.rim = $('.rimlite'); }
    if (lite.pool) q(lite.pool, ig * (.55 + .25 * flick + puff.a * .15) + fl * .6);
    if (lite.anv) q(lite.anv, ig * (.6 + .25 * flick + puff.a * .15) + fl);
    if (lite.shad) { q(lite.shad, ig * (.8 - .15 * flick)); const sk = `skewX(${(Math.round((Math.sin(t * 1.7) * 1.5 + (flick - .5) * 2) * 4) / 4).toFixed(2)}deg)`; if (sk !== lite.sk) lite.shad.style.transform = lite.sk = sk; }
    if (lite.rim) q(lite.rim, clamp((t - .4) / 1.6, 0, 1) * (.25 + .35 * heat) + fl * .5);
  }
  /* the camera: an idle drift, parallax with scroll, a jolt on each blow */
  const y = SY, dx = Math.sin(t * .23) * 5 + Math.sin(t * .11) * 3, dy = Math.sin(t * .19 + 1) * 3;
  const shake = shakeT < .4 ? 4 * Math.exp(-14 * shakeT) * Math.cos(55 * shakeT) : 0, pr = clamp(y / heroH, 0, 1.3);
  foreY = y * .08 + dy + shake; sceneScale = 1 + pr * .05;
  put(layer.far, dx * .3, y * .34 + dy * .3); put(layer.mid, dx * .6, y * .17 + dy * .6); put(layer.fore, dx, foreY); put(layer.near, dx * 1.6, -y * .12 + dy * 1.4);
  const ss = `scale(${sceneScale.toFixed(3)})`; if (scene._t !== ss) scene.style.transform = scene._t = ss;
  Sky.haze = { x0: sceneRect.left + 470 * k, x1: sceneRect.left + 1000 * k, y0: sceneRect.top - 80, y1: sceneRect.top + 330 * k, a: ig };
}

function q(el, v) {          /* opacity in steps of .03, written only on change: a whole-scene layer recomposites whenever it is touched */
  const s = (Math.round(v / .03) * .03).toFixed(2); if (el._o !== s) el.style.opacity = el._o = s;
}
function put(el, x, y) {     /* half-pixel steps: a drift of a hundredth of a pixel a frame would recomposite the whole scene for nothing */
  const v = `translate3d(${(Math.round(x * 2) / 2)}px,${(Math.round(y * 2) / 2)}px,0)`;
  if (el._t !== v) el.style.transform = el._t = v;
}

/* ---------- the sky: turning about the pole star, drawn at 30 fps; sparks that climb high become 1 px stars ---------- */
const Sky = (() => {
  const cv = $('#sky'), c = cv.getContext('2d');
  let W = 0, H = 0, d = 1, px = 0, py0 = 0, py = 0, R = 1, sd = 11, shoot = null, nextShoot = 12 + Math.random() * 20, dirty = true, lastRot = 0, lastPy = 0, lastHv = 1;
  const r2 = () => (sd = (sd * 16807) % 2147483647) / 2147483647;
  const field = Array.from({ length: 1300 }, () => ({ r: Math.sqrt(r2()) * 1.75, a: r2() * 6.283, m: Math.pow(r2(), 2.6), tw: r2() * 6.28, c: r2(), s: 1 + r2() * 2 }));
  const OT = [[11.062, 61.75], [11.031, 56.38], [11.897, 53.69], [12.257, 57.03], [12.9, 55.96], [13.399, 54.93], [13.792, 49.31]];
  const OL = [[0, 1], [1, 2], [2, 3], [3, 0], [3, 4], [4, 5], [5, 6]];
  const born = [];
  function size() { W = innerWidth; H = innerHeight; d = capDpr(W, H); cv.width = Math.round(W * d); cv.height = Math.round(H * d); px = W * (W > 900 ? .74 : .78); py0 = H * (W > 900 ? .1 : .06); R = Math.hypot(W, H) * .72; dirty = true; }
  let rot = 0;
  /* an ember let go by the scene, already scattered across the convection column, settles as a star */
  function bear(x, y, salt) {
    if (DAY || x < 0 || x > W || y < 0 || y > H) return;
    const dx = x - px, dy = (y - py) / .9; born.push({ r: Math.hypot(dx, dy) / R, a: Math.atan2(dy, dx) - rot, b: tNow, m: .3 + Math.random() * .45, salt }); if (born.length > 30) born.shift();
  }
  let tNow = 0;
  let hv = 1;
  function step(dt, t) {
    tNow = t; rot = t * 2 * Math.PI / 1200 + SY * .00035;          /* one turn in twenty minutes, and the scroll turns it too */
    py = py0 - Math.min(SY * .3, H * .8);                 /* the pole rides up with the hero and leaves the view */
    hv = 1 - clamp((SY - heroH * .3) / (heroH * .5), 0, 1); /* the pole, Otava and spark-stars belong to the hero */
    if (!shoot && t > nextShoot) { const x = W * (.3 + Math.random() * .6), y = H * (.05 + Math.random() * .25), a = Math.atan2(y - py, x - px) + Math.PI / 2; shoot = { x, y, dx: Math.cos(a), dy: Math.sin(a), t0: t }; nextShoot = t + 32 + Math.random() * 16; }
  }
  /* redraw only when something visible changed: the turn moved the farthest star by half a pixel, embers fly, a star was born or shoots */
  function due(t) {
    if (DAY) { const d0 = dirty; dirty = false; return d0; }   /* by day the sky is empty: clear it once, then leave it */
    const far = Math.hypot(Math.max(px, W - px), Math.max(py, H - py)), moved = Math.abs(rot - lastRot) * far + Math.abs(py - lastPy);
    if (dirty || moved >= .5 || shoot || Math.abs(hv - lastHv) > .02 || (born.length && t - born[born.length - 1].b < .15)) { dirty = false; lastRot = rot; lastPy = py; lastHv = hv; return true; }
    return false;
  }
  const hz = (x, y, t, h) => (h && x > h.x0 && x < h.x1 && y > h.y0 && y < h.y1) ? Math.sin(t * 4.2 + y * .07) * 1.6 * h.a * Math.sin(Math.PI * (x - h.x0) / (h.x1 - h.x0)) : 0;
  function star(x, y, r) { c.beginPath(); c.moveTo(x, y - r); c.lineTo(x + r * .15, y - r * .15); c.lineTo(x + r, y); c.lineTo(x + r * .15, y + r * .15); c.lineTo(x, y + r); c.lineTo(x - r * .15, y + r * .15); c.lineTo(x - r, y); c.lineTo(x - r * .15, y - r * .15); c.closePath(); c.fill(); }
  function draw(t) {
    c.setTransform(d, 0, 0, d, 0, 0); c.clearRect(0, 0, W, H);
    const h = Sky.haze && heroOn ? Sky.haze : null;
    if (!DAY) {
      for (const s of field) {
        let x = px + Math.cos(s.a + rot) * s.r * R; const y = py + Math.sin(s.a + rot) * s.r * R * .9;
        if (x < -4 || x > W + 4 || y < -4 || y > H + 4) continue;
        x += hz(x, y, t, h);
        const tw = .72 + .28 * Math.sin(t * s.s + s.tw), al = (.16 + s.m * .8) * tw;
        c.fillStyle = s.c < .12 ? `rgba(255,214,160,${al.toFixed(3)})` : s.c < .3 ? `rgba(170,196,255,${al.toFixed(3)})` : `rgba(235,240,255,${al.toFixed(3)})`;
        const r = .55 + s.m * 1.05; c.fillRect(x - r * .5, y - r * .5, r, r);
        if (s.m > .95) { c.fillStyle = `rgba(220,230,255,${(al * .22).toFixed(3)})`; c.fillRect(x - 2.5, y - .25, 5, .5); c.fillRect(x - .25, y - 2.5, .5, 5); }
      }
      const pd = .15 + .85 * hv, ko = hv * (1 - clamp((SY - H * .15) / (H * .55), 0, 1));
      if (ko > .01) {
        const S = OT.map(([ra, de]) => { const ox = -(ra - 12.4) * 15 * Math.cos(de * Math.PI / 180), oy = -(de - 56), rr = Math.hypot(ox, oy), aa = Math.atan2(oy, ox) + rot * .6; return [px - 260 + Math.cos(aa) * rr * 15, py + 30 + Math.sin(aa) * rr * 15]; });
        c.strokeStyle = `rgba(212,174,102,${(.3 * ko).toFixed(3)})`; c.lineWidth = .8; c.beginPath();
        for (const [a, b] of OL) { c.moveTo(S[a][0], S[a][1]); c.lineTo(S[b][0], S[b][1]); } c.stroke();
        for (const [x, y] of S) { c.fillStyle = `rgba(255,244,220,${(.95 * ko).toFixed(3)})`; c.fillRect(x - 1.1, y - 1.1, 2.2, 2.2); c.fillStyle = `rgba(240,211,146,${(.35 * ko).toFixed(3)})`; c.fillRect(x - 3.5, y - .25, 7, .5); c.fillRect(x - .25, y - 3.5, .5, 7); }
      }
      for (const s of hv > .01 ? born : []) {
        let x = px + Math.cos(s.a + rot) * s.r * R; const y = py + Math.sin(s.a + rot) * s.r * R * .9;
        if (x < -4 || x > W + 4 || y < -4 || y > H + 4) continue;
        x += hz(x, y, t, h);
        /* a spark settles as a star: a two-frame flash, then a point that cools over 6 s to the faint stars round it */
        const age = t - s.b, kk = clamp(age / 6, 0, 1), tw = .75 + .25 * Math.sin(t * 2.2 + s.a * 7), col = s.salt ? SALT[s.salt] : [255, 236, 200];
        const cc = [lerp(255, col[0] * .3 + 235 * .7, kk), lerp(214, col[1] * .3 + 240 * .7, kk), lerp(150, col[2] * .3 + 255 * .7, kk)];
        if (age < .07) { c.fillStyle = css(cc, .4 * hv); c.fillRect(x - 1.5, y - 1.5, 3, 3); }
        const r = lerp(1.4, .6 + s.m * .5, kk), al = lerp(.75, .14 + s.m * .3, kk) * tw * hv;
        c.fillStyle = css(cc, al); c.fillRect(x - r * .5, y - r * .5, r, r);
      }
      if (shoot) {
        const u = (t - shoot.t0) / .9;
        if (u > 1) shoot = null;
        else { const x = shoot.x + shoot.dx * u * 280, y = shoot.y + shoot.dy * u * 280, ex = x - shoot.dx * 120, ey = y - shoot.dy * 120, g = c.createLinearGradient(x, y, ex, ey); g.addColorStop(0, `rgba(240,244,255,${(.9 * Math.sin(Math.PI * u)).toFixed(3)})`); g.addColorStop(1, 'rgba(240,244,255,0)'); c.strokeStyle = g; c.lineWidth = 1.2; c.beginPath(); c.moveTo(x, y); c.lineTo(ex, ey); c.stroke(); }
      }
      /* the pole star: the mark's engraved star, the still point the sky turns on */
      const pz = 1 + .12 * Math.pow(Math.max(0, Math.sin(t * 1.4)), 6);
      const g = c.createRadialGradient(px, py, 0, px, py, 34); g.addColorStop(0, `rgba(240,211,146,${(.3 * pd).toFixed(3)})`); g.addColorStop(1, 'rgba(240,211,146,0)');
      c.fillStyle = g; c.fillRect(px - 34, py - 34, 68, 68);
      c.globalAlpha = .3 + .7 * pd; c.fillStyle = '#F0D392'; star(px, py, 12 * pz); c.globalAlpha = 1;
    }
  }
  addEventListener('resize', size);
  skyTouch = () => (dirty = true);
  return { bear, step, draw, size, due, haze: null };
})();

/* ---------- the camera carries on through the stations: each figure is pushed in as it arrives ---------- */
const figs = new Set(), fo = new IntersectionObserver(es => es.forEach(e => (e.isIntersecting ? figs.add(e.target) : figs.delete(e.target))));
const watchFigs = () => $$('.side .fig, .side .chart').forEach(f => fo.observe(f));
let scrolled = true, SY = 0; addEventListener('scroll', () => (scrolled = true), { passive: true });
function camera() {
  if (!scrolled) return; scrolled = false;
  const vh = innerHeight, rs = [...figs].map(f => [f, f.getBoundingClientRect()]);
  for (const [f, r] of rs) { const p = clamp((vh - r.top) / (vh * .85), 0, 1), e = 1 - Math.pow(1 - p, 3); f.style.transform = `translate3d(0,${((1 - e) * 26).toFixed(1)}px,0) scale(${(1.07 - .07 * e).toFixed(4)})`; }
}

let heroH = 800;
function resize() { sizeFx(); sizeFire(); sceneRect = scene.getBoundingClientRect(); heroH = hero.offsetHeight; scrolled = true; }
addEventListener('resize', resize); requestAnimationFrame(() => requestAnimationFrame(() => { Sky.size(); resize(); }));
setTimeout(watchFigs, 1500); setTimeout(watchFigs, 3500);
function loop(now) {
  if (document.hidden || now - lastIn > LIMIT) { asleep = true; root.classList.add('rest'); return; }
  if (!t0) { t0 = now; last = now; }
  const t = (now - t0) / 1000, dt = Math.min(.05, (now - last) / 1000); last = now; frameN++;
  if (scrolled) SY = scrollY;   /* read once, before any write: a read after a write forces a style and layout pass */
  if (heroOn) { if (scrolled) sceneRect = scene.getBoundingClientRect(); frameHero(t, dt); }
  Sky.step(dt, t); if (frameN % 2 === 0 && Sky.due(t)) Sky.draw(t);
  camera();
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
})();
