"""fm shot (T-1636): what a page looks like, from a headless Chromium this machine already has (FOREMAN_CHROME, one on
PATH, or Playwright's download; nothing is ever installed). fm shot URL|FILE... [--frames N] [--every MS] [--frozen]
[--out DIR] writes, per page, viewport.png once it has loaded, frame-00.png… N frames --every ms apart in real time
(headless virtual time barely runs requestAnimationFrame) and, when the frames differ, poster.png: the frame with the
most painted pixels (those off the frame's commonest colour). --frozen applies fm smoke's frozen profile first (the
clock fixed at 2026-01-15 10:00 UTC, CSS animations and transitions paused), so the frames should match. Shots go in
<project state>/shots/<page>/ unless --out names a folder (never a link); a rerun replaces its own files. Without a
browser it exits 0 and says why.

The browser is driven over --remote-debugging-pipe (fd 3 in, fd 4 out, NUL-separated JSON): stdlib only, the way
test_site_hero's devtools() does (T-1610)."""
import atexit
import base64
import collections
import contextlib
import glob
import json
import os
import pathlib
import re
import select
import shutil
import signal
import sys
import tempfile
import time
import unicodedata

from sampo import core as c
from sampo import img as simg

SIZE = (1280, 800)
MAX_FRAMES, MIN_EVERY, MAX_EVERY = 60, 10, 10000
MAX_WIDTH, MAX_VIEWS = 10000, 12  # --width: a viewport's width, and how many of them one run shoots per page
LOAD_S, CALL_S, SETTLE_S = 30, 30, 0.5
FONTS_S = 10  # the fonts check: its own 5 s wait plus the walk; a slow page leaves fonts unchecked, not the run hung
NOISY = 0.05  # a baseline floor over this share of the page's pixels: an animated baseline, a loose pass line
MAX_MSG = 64 << 20  # one DevTools message: a screenshot's base64 is a few MB at most
LOCK_S, CLOSE_S = 120, 5
WATCH = {"Page.loadEventFired"}  # the only events kept (a page can send endless others)
FROZEN_MS = 1768471200000  # 2026-01-15T10:00:00Z, smoke_web.cjs's NOW
# smoke_web.cjs's freeze(): CSS animations and transitions held still (test_fmshot keeps the two in step)
FREEZE_CSS = ("*, *::before, *::after { animation-play-state: paused !important; transition: none !important; "
              "caret-color: transparent !important; scroll-behavior: auto !important }")
FREEZE = """(() => {
  const D = Date, T = %d;
  function F(...a) { return new.target ? new D(...(a.length ? a : [T])) : new D(T).toString(); }
  F.prototype = D.prototype; F.now = () => T; F.parse = D.parse; F.UTC = D.UTC;
  window.Date = F;
  const put = () => { const s = document.createElement("style"); s.textContent = %s; document.documentElement.appendChild(s); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", put); else put();
})();""" % (FROZEN_MS, json.dumps(FREEZE_CSS))
# Headless Chromium keeps running a CSS animation under the paused rule until something recalculates style (even a
# pause() from a load listener didn't hold it); asked once the page has loaded, getAnimations() does. A finite
# animation (Web Animations too) is finished, so a `fadein .6s forwards` hero shows its end state; an endless one is
# held at its start. Either way a frozen shot is the same on every run.
HOLD = ("document.getAnimations().forEach(a => { try { const t = a.effect && a.effect.getComputedTiming(); "
        "if (t && isFinite(t.endTime)) a.finish(); else { a.pause(); a.currentTime = 0; } } catch (e) { a.pause(); } })")
# T-1637: the font families the page drew text in a fallback for, once its fonts settle (5 s at most): a family that
# visible text names first (its element's own non-blank text, outside script/style, checkVisibility() true; the first
# 20,000 text nodes) with a face that failed or none loaded. A declared face no visible text uses isn't one, so a
# canvas page with body{font-family:X} passes. ponytail: a family that's neither declared nor installed, or canvas
# text in a failed font, falls back unseen (document.fonts lists only @font-face faces).
FONTS = """Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 5000))]).then(() => {
  const bare = s => s.trim().replace(/^["']|["']$/g, "").toLowerCase(), used = new Set(), faces = new Map();
  const walk = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  for (let n, i = 0; (n = walk.nextNode()) && i < 20000; i++) {
    const el = n.parentElement;
    if (!el || !n.data.trim() || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TITLE)$/.test(el.tagName)) continue;
    if (el.checkVisibility ? !el.checkVisibility({visibilityProperty: true}) : !el.getClientRects().length) continue;
    used.add(bare(getComputedStyle(el).fontFamily.split(",")[0]));
  }
  document.fonts.forEach(f => { const k = f.family.trim().replace(/^["']|["']$/g, "");
    faces.set(k, (faces.get(k) || []).concat(f.status)); });
  const bad = [];
  faces.forEach((st, fam) => { if (used.has(bare(fam)) && (st.includes("error") || !st.includes("loaded")))
    bad.push({family: fam, status: [...new Set(st)].join("/")}); });
  return bad;
})"""
OURS = re.compile(r"(viewport|poster|frame-\d+)\.png")
NAMES = ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome")
NONE = ("no browser found (looked for FOREMAN_CHROME, chrome-headless-shell, chromium, chromium-browser or "
        "google-chrome on PATH, and Playwright's download in PLAYWRIGHT_BROWSERS_PATH or ~/.cache/ms-playwright); "
        "fm shot installs nothing: set FOREMAN_CHROME=<a Chromium binary> or install one yourself")


def _usable(p):
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def chrome():
    """A headless Chromium: FOREMAN_CHROME, one on PATH, or Playwright's newest download; None if absent or
    FOREMAN_CHROME=none (a browser that starts, then crashes: T-1806)."""
    if os.environ.get("FOREMAN_CHROME") == "none":
        return None
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    root = root if root and root != "0" else os.path.expanduser("~/.cache/ms-playwright")  # 0: inside node_modules
    builds = glob.glob(os.path.join(root, "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"))
    build = lambda p: int((re.search(r"chromium_headless_shell-(\d+)/", p) or (0, -1))[1])  # noqa: E731
    found = [os.environ.get("FOREMAN_CHROME")] + [shutil.which(n) for n in NAMES] + sorted(builds, key=build,
                                                                                         reverse=True)
    return next((p for p in found if _usable(p)), None)


def painted(img):
    """Pixels off the image's commonest colour (its background)."""
    px = memoryview(img.rgba).cast("I")
    return len(px) - collections.Counter(px).most_common(1)[0][1] if len(px) else 0


def _safe(text):
    """A URL or path for the terminal: every control, format (zero-width, bidi, tag, soft hyphen), private-use or
    surrogate character a ? (T-1642)."""
    return "".join("?" if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs") else ch for ch in str(text))


safe, usable = _safe, _usable  # the names other verbs use (fm flow)


def find_browser(who):
    """(path, note) for verb `who`: a browser and a note when FOREMAN_CHROME was set but unusable, or (None, why)."""
    env = os.environ.get("FOREMAN_CHROME")
    bad = f"FOREMAN_CHROME={_safe(env)!r} isn't an executable file" if env and not _usable(env) else None
    path = chrome()
    return (path, bad) if path else (None, (bad + "; " if bad else "") + NONE.replace("fm shot", who))


@contextlib.contextmanager
def killable():
    """SIGTERM unwinds (SystemExit) while this holds, so a browser's group dies and its profile goes."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    old = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


class Browser:
    """One headless Chromium over the DevTools pipe; close() kills its whole process group (its own group, not
    setsid: a Python built on an old glibc, uv's python-build-standalone, has no POSIX_SPAWN_SETSID; T-1799)."""

    def __init__(self, path, size, scale=1, reduced=False, env=None, args=()):
        """env: the browser's environment (None: ours); args: more switches (sampo works: no network, T-3106)."""
        import fcntl
        self.pid, self.feed, self.out, fds = None, None, None, []
        self.profile = tempfile.TemporaryDirectory(prefix="fm-shot-")
        self.log = os.path.join(self.profile.name, "fm-shot-stderr.log")
        self.buf, self.scan, self.n, self.events = bytearray(), 0, 0, set()
        self.on = {}  # event method -> handler(params), run as the event is read (fm flow answers Fetch.requestPaused)
        try:
            child_in, self.feed = os.pipe()
            fds.append(child_in)
            self.out, child_out = os.pipe()
            fds.append(child_out)
            fds += [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10) for fd in (child_in, child_out)]
            self.pid = os.posix_spawn(path, [
                path, "--headless", "--remote-debugging-pipe", "--user-data-dir=" + self.profile.name,
                "--no-first-run", "--hide-scrollbars", "--mute-audio", "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader", f"--window-size={size[0]},{size[1]}", *args, "about:blank"],
                os.environ if env is None else env,
                setpgroup=0, file_actions=[(os.POSIX_SPAWN_DUP2, fds[2], 3), (os.POSIX_SPAWN_DUP2, fds[3], 4),
                                           (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                                           (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                                           (os.POSIX_SPAWN_OPEN, 2, self.log, os.O_WRONLY | os.O_CREAT, 0o600)])
            for fd in fds:
                os.close(fd)
            fds = []
            tid = self.call("Target.createTarget", url="about:blank")["targetId"]
            self.sid = self.call("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
            self.call("Page.enable", self.sid)
            self.call("Emulation.setDeviceMetricsOverride", self.sid, width=size[0], height=size[1],
                      deviceScaleFactor=scale, mobile=False)
            if reduced:  # before any navigation: the page's first paint already sees it
                self.call("Emulation.setEmulatedMedia", self.sid,
                          features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        except BaseException as e:  # never leave a browser, a pipe or the profile behind a failed start
            last = self.last_words()
            for fd in fds:
                os.close(fd)
            self.close()
            if last and isinstance(e, (OSError, RuntimeError, EOFError, ValueError)):
                raise RuntimeError(f"{e}; its last words: {last}") from e
            raise

    def emulate(self, size, scale, scheme=None, reduced=False):
        """Another view in the same browser. setEmulatedMedia replaces the whole feature list: send all of it."""
        self.call("Emulation.setDeviceMetricsOverride", self.sid, width=size[0], height=size[1],
                  deviceScaleFactor=scale, mobile=False)
        feats = ([{"name": "prefers-color-scheme", "value": scheme}] if scheme else []) + \
                ([{"name": "prefers-reduced-motion", "value": "reduce"}] if reduced else [])
        self.call("Emulation.setEmulatedMedia", self.sid, features=feats)

    def last_words(self):
        """The browser's last stderr lines (why it didn't start), capped."""
        try:
            with open(self.log, "rb") as f:
                f.seek(max(0, os.fstat(f.fileno()).st_size - 2000))
                lines = f.read().decode("utf-8", "replace").strip().splitlines()[-3:]
        except OSError:
            return ""
        return _safe(" / ".join(x.strip() for x in lines))[:500]

    def _next(self, deadline, what):
        """The next message; TimeoutError past `deadline` even while messages keep coming."""
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no answer to {what} in time")
            end = self.buf.find(b"\0", self.scan)
            if end >= 0:
                line = bytes(self.buf[:end])
                del self.buf[:end + 1]
                self.scan = 0
                return json.loads(line)
            self.scan = len(self.buf)  # the next search starts where this one stopped
            if len(self.buf) > MAX_MSG:
                raise RuntimeError(f"an answer over {MAX_MSG >> 20} MB during {what}")
            if not select.select([self.out], [], [], max(0.0, deadline - time.monotonic()))[0]:
                continue
            chunk = os.read(self.out, 1 << 16)
            if not chunk:
                raise EOFError(f"the browser closed during {what}")
            self.buf += chunk

    def call(self, method, session=None, _within=None, **params):
        """The answer's result; TimeoutError after _within seconds (CALL_S by default). A late answer is skipped."""
        mine = self.send(method, session, **params)
        deadline = time.monotonic() + (CALL_S if _within is None else _within)
        while True:
            got = self._next(deadline, method)
            if got.get("id") == mine:
                if "error" in got:
                    raise RuntimeError(f"{method}: {got['error'].get('message', got['error'])}")
                return got.get("result") or {}
            self._event(got)

    def send(self, method, session=None, **params):
        """Ask without waiting for the answer (it is skipped when it comes); its id."""
        if "url" in params:  # T-3691: run.py's test recorder judges each page whether it may load the repo's files
            sys.audit("sampo.page", params["url"])
        self.n += 1
        os.write(self.feed, json.dumps({"id": self.n, "method": method, "params": params,
                                        **({"sessionId": session} if session else {})}).encode() + b"\0")
        return self.n

    def _event(self, got):
        if got.get("method") in WATCH:
            self.events.add(got["method"])
        if got.get("method") in self.on:
            self.on[got["method"]](got.get("params") or {})

    def wait(self, event, seconds):
        """True once `event` has come (since the events were last cleared), False after `seconds`."""
        deadline = time.monotonic() + seconds
        try:
            while event not in self.events:
                self._event(self._next(deadline, event))
            return True
        except TimeoutError:
            return False
        finally:
            self.events.clear()

    def png(self):
        return base64.b64decode(self.call("Page.captureScreenshot", self.sid, format="png")["data"])

    def close(self):
        if self.pid:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:  # the group can't be signalled: the browser itself, at least
                with contextlib.suppress(OSError):
                    os.kill(self.pid, signal.SIGKILL)
            end = time.monotonic() + CLOSE_S  # a browser that can't be killed is left, not waited on forever
            with contextlib.suppress(ChildProcessError):
                while not os.waitpid(self.pid, os.WNOHANG)[0] and time.monotonic() < end:
                    time.sleep(0.02)
            self.pid = None
        for fd in (self.feed, self.out):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self.feed = self.out = None
        self.profile.cleanup()


_SHARED = {}  # browser path -> the Browser this process shares; after a kill -9 (no atexit) Chromium exits itself when
# its DevTools pipe closes (test_a_killed_test_process_leaves_no_browser_behind)


class Lease:
    """One test's page in a shared Browser (T-3296): its own context (no cookies, storage or handlers from the last
    user); close() drops the context and leaves the browser. Everything else is the Browser's."""

    def __init__(self, b, size, scale, reduced):
        if getattr(b, "leased", False):  # one session id, one handler table: two live leases would trample each other
            raise AssertionError("shot.shared: the browser already has a live lease; close it first")
        self.b = b
        b.on.clear()
        b.events.clear()
        self.ctx = b.call("Target.createBrowserContext")["browserContextId"]
        try:
            tid = b.call("Target.createTarget", url="about:blank", browserContextId=self.ctx)["targetId"]
            b.sid = b.call("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
            b.call("Page.enable", b.sid)
            b.emulate(size, scale, reduced=reduced)
        except BaseException:  # no half-built lease keeps a context alive in the shared browser
            with contextlib.suppress(OSError, RuntimeError, EOFError, ValueError):
                b.call("Target.disposeBrowserContext", browserContextId=self.ctx)
            raise
        b.leased = True

    def __getattr__(self, name):
        return getattr(self.b, name)

    def close(self):
        self.b.leased = False
        self.b.on.clear()
        with contextlib.suppress(OSError, RuntimeError, EOFError, ValueError):  # a dead browser has no context to drop
            self.b.call("Target.disposeBrowserContext", browserContextId=self.ctx)


def shared(path, size, scale=1, reduced=False):
    """A Lease on this process's one browser for `path`, launched on first use and again if it died."""
    for _ in range(2):
        b = _SHARED.get(path)
        if b is None:
            b = _SHARED[path] = Browser(path, size, scale, reduced)
        try:
            return Lease(b, size, scale, reduced)
        except (OSError, RuntimeError, EOFError, ValueError):  # crashed or hung between uses: start over once
            _SHARED.pop(path, None).close()
    b = _SHARED[path] = Browser(path, size, scale, reduced)
    try:
        return Lease(b, size, scale, reduced)
    except BaseException:  # a browser that can't even lease is not kept for the next test
        _SHARED.pop(path, None)
        b.close()
        raise


def close_shared():
    while _SHARED:
        _SHARED.popitem()[1].close()


atexit.register(close_shared)


def _url(target):
    from sampo import cli
    if re.match(r"(?i)(https?|file)://", target):
        return target
    if not os.path.isfile(target):  # javascript:, data:, chrome: and the rest are refused here too
        raise cli.UsageError(f"fm shot takes an http(s) or file URL or an existing file: {_safe(target)!r}")
    return pathlib.Path(os.path.abspath(target)).as_uri()


def _slug(url, taken):
    """The page's folder name: a file's name without its extension, or host and path; lower case, unique per run."""
    m = re.match(r"(?i)[a-z]+://([^/?#]*)([^?#]*)", url)
    if not m:
        raw = url
    elif url.lower().startswith("file:"):
        raw = os.path.splitext(os.path.basename(m[2]))[0]
    else:
        raw = m[1] + os.path.splitext(m[2])[0]
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-.")[:60] or "page"
    name, i = slug, 1
    while name in taken:
        i += 1
        name = f"{slug}-{i}"
    taken.add(name)
    return name


def _folder(base, name):
    """The page's folder, the last run's shots cleared; OSError for a link (that page fails, the rest go on)."""
    folder = os.path.join(base, name)
    os.makedirs(folder, exist_ok=True)
    if os.path.islink(folder):
        raise OSError(f"{folder} is a link: fm shot never writes through one")
    for f in os.listdir(folder):  # a rerun leaves none of the last run's frames behind
        if OURS.fullmatch(f):
            os.unlink(os.path.join(folder, f))
    return folder


def _write(folder, name, data):
    path = os.path.join(folder, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)  # cleared just before
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def fonts(b):
    """[{family, status}] the page drew in a fallback font (FONTS), at most 20; None if the page wouldn't say in
    FONTS_S (a closed browser still raises: the next call would fail anyway)."""
    try:
        got = b.call("Runtime.evaluate", b.sid, min(FONTS_S, CALL_S), expression=FONTS, awaitPromise=True,
                     returnByValue=True)
    except (RuntimeError, TimeoutError):
        return None
    found = (got.get("result") or {}).get("value")
    if "exceptionDetails" in got or not isinstance(found, list):
        return None
    return [{"family": str(f.get("family"))[:100], "status": str(f.get("status"))[:40]}
            for f in found[:20] if isinstance(f, dict)]


def floor(folder):
    """(the baseline's viewport shot, its noise floor, its frame count): the floor is the most pixels any of its
    frames differs from that shot, 0 without frames. ponytail: pure-Python diffs, about a second a megapixel each."""
    view = simg.load(os.path.join(folder, "viewport.png"))
    frames = sorted(f for f in os.listdir(folder) if re.fullmatch(r"frame-\d+\.png", f))
    return view, max((simg.diff(view, simg.load(os.path.join(folder, f)))["changed"] for f in frames),
                     default=0), len(frames)


def compare(page, base):
    """The page's viewport shot against BASE/<its folder's name>/: {baseline, changed, total, floor, frames, bbox,
    ok}, or {baseline, error, ok: False} when the baseline is missing, unreadable, another size or the page's own
    folder (through a link)."""
    ref = os.path.join(base, os.path.basename(page["folder"]))
    try:
        if os.path.realpath(ref) == os.path.realpath(page["folder"]):
            raise ValueError(f"{ref} is this page's own folder (a link): the baseline was cleared before the shot")
        view, noise, n = floor(ref)
        d = simg.diff(view, simg.load(page["shots"][0]))
    except (OSError, ValueError) as e:
        return {"baseline": ref, "error": str(e), "ok": False}
    return {"baseline": ref, "changed": d["changed"], "total": d["total"], "floor": noise, "frames": n,
            "bbox": d["bbox"], "ok": d["changed"] <= noise}


def load(b, url):
    """Open url and wait for its load event (LOAD_S at most), then SETTLE_S: notes. fm shot and fm critique share it."""
    b.events.clear()  # the last page's load is not this one's
    nav = b.call("Page.navigate", b.sid, url=url)
    if nav.get("errorText"):
        raise RuntimeError(f"did not load: {nav['errorText']}")
    notes = [] if b.wait("Page.loadEventFired", LOAD_S) else [f"still loading after {LOAD_S} s; taken as it stood"]
    b.wait("fm-shot.settle", SETTLE_S)  # an event that never comes: held requests are answered meanwhile
    return notes


def shoot(b, url, folder, frames, every, frozen):
    """One page: {url, folder, shots, animated, poster_of, fallback_fonts, notes}."""
    notes = load(b, url)
    fell = fonts(b)
    if frozen:
        b.call("Runtime.evaluate", b.sid, expression=HOLD)
    shots = [_write(folder, "viewport.png", b.png())]
    pngs, start = [], time.monotonic()
    for i in range(frames):
        time.sleep(max(0.0, start + i * every / 1000 - time.monotonic()))
        pngs.append(b.png())
        shots.append(_write(folder, f"frame-{i:02d}.png", pngs[-1]))
    animated, poster_of = len(set(pngs)) > 1, None
    if animated:  # ponytail: pure-Python decode, about a second a megapixel per frame; fine at a viewport's size
        counts = [painted(simg.decode(x)) for x in pngs]
        best = counts.index(max(counts))
        poster_of = f"frame-{best:02d}.png"
        shots.append(_write(folder, "poster.png", pngs[best]))
        if frozen:
            notes.append("still moves with the clock frozen and CSS animations paused (a canvas, video, timer or "
                         "requestAnimationFrame loop)")
    return {"url": url, "folder": folder, "shots": shots, "animated": animated, "poster_of": poster_of,
            "fallback_fonts": fell, "notes": notes}


def view(args):
    """(size, scale) for --profile (fm critique's table), else the default viewport."""
    from sampo import critique
    p = critique.PROFILES.get(getattr(args, "profile", None))
    return ((p["width"], p["height"]), p["scale"]) if p else (SIZE, 1)


def views(args):
    """[(width|None, scheme|None)] from --width W[,W…] and --scheme: widths outer, light before dark; [(None, None)]
    with neither. UsageError for a width that isn't a whole number 1 to MAX_WIDTH or over MAX_VIEWS widths."""
    from sampo import cli
    raw = getattr(args, "width", None)
    widths = [None]
    if raw is not None:
        parts = [x.strip() for x in raw.split(",")]
        if not all(x.isascii() and x.isdigit() and 1 <= int(x) <= MAX_WIDTH for x in parts) or len(parts) > MAX_VIEWS:
            raise cli.UsageError(f"--width is up to {MAX_VIEWS} whole numbers 1 to {MAX_WIDTH}, comma-separated: "
                                 f"{_safe(raw)!r}")
        widths = [int(x) for x in parts]
    scheme = getattr(args, "scheme", None)
    return [(w, s) for w in widths for s in ({"both": ["light", "dark"], None: [None]}.get(scheme) or [scheme])]


def _run(path, urls, base, args):
    """Every page through one browser: (pages, failed). A view per --width and --scheme, in PAGE-W-SCHEME/."""
    pages, failed, taken = [], [], set()
    try:
        size, scale = view(args)
        b = Browser(path, size, scale, getattr(args, "reduced_motion", False))
    except (OSError, RuntimeError, EOFError, ValueError) as e:  # TimeoutError is an OSError
        return pages, [{"url": ", ".join(urls), "error": f"the browser ({path}) did not start: {e}"}]
    try:
        if args.frozen:
            b.call("Page.addScriptToEvaluateOnNewDocument", b.sid, source=FREEZE)
        matrix, dead = views(args), False
        for url in urls:
            slug = _slug(url, taken)
            for width, scheme in matrix:
                name = "-".join(str(x) for x in (slug, width, scheme) if x)
                try:
                    if width or scheme:
                        b.emulate((width or size[0], size[1]), scale, scheme, getattr(args, "reduced_motion", False))
                    pages.append(shoot(b, url, _folder(base, name), args.frames, args.every, args.frozen))
                    if width or scheme:
                        pages[-1].update(width=width or size[0], scheme=scheme)
                    if args.diff is not None:
                        pages[-1]["diff"] = compare(pages[-1], args.diff)
                except (OSError, RuntimeError, EOFError, ValueError) as e:
                    failed.append({"url": url, "error": str(e)})
                    if isinstance(e, (EOFError, TimeoutError)):  # the browser is gone or hung: the rest would wait too
                        failed += [{"url": u, "error": "not tried: the browser stopped answering"}
                                   for u in urls[urls.index(url) + 1:]]
                        dead = True
                        break
            if dead:
                break
    except (OSError, RuntimeError, EOFError, ValueError) as e:
        failed.append({"url": ", ".join(urls), "error": f"the browser ({path}) failed: {e}"})
    finally:
        b.close()
    return pages, failed


def cmd_shot(args):
    from sampo import cli
    if not 0 <= args.frames <= MAX_FRAMES:
        raise cli.UsageError(f"--frames is 0 to {MAX_FRAMES}")
    if not MIN_EVERY <= args.every <= MAX_EVERY:
        raise cli.UsageError(f"--every is {MIN_EVERY} to {MAX_EVERY} ms")
    views(args)  # a bad --width is refused before anything starts
    urls = [_url(t) for t in args.targets]
    if args.out:
        base = os.path.abspath(args.out)
        if os.path.islink(base):
            raise cli.UsageError(f"--out {_safe(base)} is a link: name a real folder")
        if os.path.exists(base) and not os.path.isdir(base):
            raise cli.UsageError(f"--out {_safe(base)} isn't a folder")
    else:
        base = os.path.join(cli.resolve(args).dir, "shots")
    if args.diff is not None:  # "" too: a gate given an empty BASE must not pass having compared nothing
        if not args.diff.strip() or not os.path.isdir(args.diff):
            raise cli.UsageError(f"--diff {_safe(args.diff)!r} isn't a folder: name an earlier fm shot --out")
        args.diff = os.path.abspath(args.diff)
        mine, theirs = os.path.realpath(base), os.path.realpath(args.diff)
        if theirs == mine or theirs.startswith(mine + os.sep):  # a page's folder is cleared before it's shot
            raise cli.UsageError(f"--diff {_safe(args.diff)} is where these shots go and would be cleared: "
                                   "shoot into another --out")
    path, bad = find_browser("fm shot")
    if not path:
        why = bad
        if args.diff is None:
            return cli.out(args, {"skipped": why}, f"fm shot: skipped: {why}.")
        cli.out(args, {"skipped": why, "compared": False}, f"✗ fm shot --diff: not compared: {why}.")
        sys.exit(1)
    os.makedirs(base, exist_ok=True)
    # a second run into Foreman's folder waits for the first; --out is the user's folder: no .lock left in it
    with killable(), contextlib.nullcontext() if args.out else c.lock(base, timeout=LOCK_S):
        pages, failed = _run(path, urls, base, args)
    lines = [f"note: {bad}; used {_safe(path)}"] if bad else []
    for pg in pages:
        names = [os.path.basename(s) for s in pg["shots"]]
        frames = [n for n in names if n.startswith("frame-")]
        lines.append(f"fm shot: {_safe(pg['url'])} → {_safe(pg['folder'])}")
        lines.append("  viewport.png")
        if frames:
            lines.append(f"  {frames[0]}" + (f" … {frames[-1]}" if len(frames) > 1 else "")
                         + f" ({len(frames)} frames {args.every} ms apart, real time"
                         + (", frozen clock" if args.frozen else "") + ")")
        if pg["poster_of"]:
            lines.append(f"  poster.png ({pg['poster_of']}: the most painted of {len(frames)})")
        elif len(frames) > 1:
            lines.append("  no poster: the page held still")
        lines += [f"  note: {n}" for n in pg["notes"]]
        mark = "  ✗ " if args.diff else "  note: "  # under --diff a font problem fails the run
        if pg["fallback_fonts"] is None:
            lines.append(mark + "fonts couldn't be checked (document.fonts didn't answer in time)")
        elif pg["fallback_fonts"]:
            lines.append(mark + "fonts fell back: " + ", ".join(
                f"{_safe(f['family'])} ({_safe(f['status'])})" for f in pg["fallback_fonts"]))
        d = pg.get("diff")
        if d and "error" in d:
            lines.append(f"  ✗ diff: no usable baseline: {_safe(d['error'])}")
        elif d and not d["changed"]:
            lines.append(f"  diff: identical to {_safe(d['baseline'])}")
        elif d:
            x0, y0, x1, y1 = d["bbox"]
            fl = f"floor {d['floor']}, from the baseline's {d['frames']} frames" if d["frames"] else \
                "floor unmeasured: the baseline has no frames, so any change fails"
            lines.append(("  diff: " if d["ok"] else "  ✗ diff: ") + f"{d['changed']} pixels changed in "
                         f"x {x0}-{x1 - 1}, y {y0}-{y1 - 1} ({fl}): " + ("within it" if d["ok"] else "over it"))
        if d and "error" not in d and d["floor"] > NOISY * d["total"]:
            lines.append(f"  note: animated baseline: its frames differ by {100 * d['floor'] / d['total']:.0f}% of "
                         "the page, so the pass line is loose; shoot the baseline and this run with --frozen")
    lines += [f"✗ {_safe(f['url'])}: {_safe(f['error'])}" for f in failed]
    cli.out(args, {"browser": path, "pages": pages, "failed": failed}, "\n".join(lines))
    if failed or (args.diff is not None and any(not p["diff"]["ok"] or p["fallback_fonts"] is None
                                                or p["fallback_fonts"] for p in pages)):
        sys.exit(1)
    return None
