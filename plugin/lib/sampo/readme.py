"""sampo readme (T-3703): a README's figures for any project, so it shows the product and how it works without anyone
asking. plan reads the repo (its page and API routes, CLI commands, entry points, services) and proposes the figures;
shots starts the app (--cmd) or uses a running one (--url), waits until it answers, and screenshots each route at
desktop (1440×900) and phone (390×844) width, light and dark when the app has a dark scheme, through shot.py's
headless Chromium; a page whose text holds a private string (secrets, the user's private terms, home paths, private
IPs, emails, internal hosts, the machine's logins) is refused and named by kind, never shot. diagram draws a spec's
nodes, edges and groups as a dark and a light SVG, laid out by rank (never by hand), in the repo's own tokens or the
Board look; check holds a README's images to their files, alt text, dark/light pairs, size budgets and private text.
The readme-visuals skill is the procedure around it."""
import html
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from sampo import core as c

VIEWS = (("desktop", (1440, 900)), ("phone", (390, 844)))
SCREENS, DIAGRAMS = os.path.join("docs", "assets", "screens"), os.path.join("docs", "assets", "diagrams")
RASTER_KB, SVG_KB = 400, 150  # check's budgets per file
READY_S, WAIT_FOR_S = 60, 15
MAX_ROUTES, MAX_NODES, MAX_EDGES, MAX_GROUPS = 12, 80, 160, 12
MAX_READ = 200_000  # bytes of one source file the plan reads
BOARD_TOKENS = os.path.join(c.PLUGIN_ROOT, "web", "live", "board-tokens.json")
ACCENTS = ("cyan", "yellow", "pink", "green", "violet", "orange")  # the Board's panel accents, in panel order


def _board():
    """The Board look (the live page's dense dark kit, T-3659) from its board-tokens.json; these values when it can't
    be read. Each light accent is dark enough for text on white, each dark one bright on the near-black."""
    try:
        with open(BOARD_TOKENS, encoding="utf-8") as f:
            kit = json.load(f)
        return {t: {"bg": kit[t]["bg"], "panel": kit[t]["panel"], "line": kit[t]["line"], "ink": kit[t]["ink"],
                    "muted": kit[t]["muted"], "accents": [kit[t][a] for a in ACCENTS]} for t in ("dark", "light")}
    except (OSError, ValueError, KeyError, TypeError):
        return {"dark": {"bg": "#0A0D13", "panel": "#0F141D", "line": "#253043", "ink": "#E6EAF2", "muted": "#8D97AB",
                         "accents": ["#22D3EE", "#F5C542", "#FF4FA0", "#3DDC84", "#9D8CFF", "#4FA3FF"]},
                "light": {"bg": "#F3F5F9", "panel": "#FFFFFF", "line": "#D3DAE6", "ink": "#0E1420", "muted": "#566176",
                          "accents": ["#0A7189", "#8A6100", "#C2186B", "#127544", "#5B47D1", "#1D63C4"]}}


BOARD = _board()


# ---------------------------------------------------------------- privacy

_INTERNAL = c.lazy(r"(?i)(?<![\w.-])(?:[a-z0-9-]+\.)+(?:home|lan|local|internal|localdomain|corp|intranet|private|test)"
                   r"(?![\w-]|\.[a-z0-9])")
_DOC_MAIL = c.lazy(r"(?i)@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|example|invalid)$")


def _mask(value):
    """Enough of a found value to find it on the page, never the whole of it."""
    return value[:max(1, min(4, len(value) // 3))] + "…"


def private_hits(text):
    """[(kind, a masked hint)] for each private string in text, a kind's first find only: secret (kind only), private
    (the user's sampo private terms), home, ip (private ranges: 10/8, 172.16/12, 192.168/16, CGNAT, link-local), email
    (not example.com/org/net), domain (an internal TLD: .home .lan .local .internal .test …) and login (the machine's)."""
    from sampo import profile
    if not isinstance(text, str) or not text:
        return []
    found = {}
    if c.secret_kinds(text):
        found["secret"] = ""  # a secret's own characters never reach the terminal
    terms = tuple(c.private_terms())
    for rx, _ in c._private_res(terms) if terms else ():
        m = rx.search(text)
        if m:
            found.setdefault("private", _mask(m.group(0)))
    home = os.path.expanduser("~").rstrip("/")
    m = c._HOMES.search(text) or c._CLAUDE_HOMES.search(text)
    if m or (len(home) > 1 and home in text):
        found["home"] = _mask(m.group(0) if m else home)
    for m in c._IPV4.finditer(text):
        a, b, *rest = (int(x) for x in m.groups())
        if max(a, b, *rest) <= 255 and (a == 10 or (a, b) in ((192, 168), (169, 254)) or a == 172 and 16 <= b <= 31
                                        or a == 100 and 64 <= b <= 127):
            found.setdefault("ip", _mask(m.group(0)))
    for m in profile._EMAIL.finditer(text):
        if not _DOC_MAIL.search(m.group(0)):
            found.setdefault("email", _mask(m.group(0)))
    m = _INTERNAL.search(text)
    if m:
        found["domain"] = _mask(m.group(0))
    rx = profile._login_re(profile._logins())
    m = rx and rx.search(text)
    if m:
        found["login"] = _mask(m.group(0))
    return sorted(found.items())


def _said(hits):
    return ", ".join(f"{k} ({v})" if v else k for k, v in hits)


# ---------------------------------------------------------------- the repo

_SKIP_DIR = c.lazy(r"(^|/)(node_modules|dist|build|vendor|third_party|\.next|\.svelte-kit|site-packages|__pycache__|"
                   r"tests?|spec|fixtures|docs?|examples?)/")
_CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".html", ".vue", ".svelte", ".astro")
_PY_ROUTE = c.lazy(r"@(\w+)\.(get|post|put|patch|delete|route|api_route|websocket)\(\s*[rbuf]?['\"]([^'\"]*)['\"]"
                   r"([^)\n]*)")
_JS_ROUTE = c.lazy(r"\b(?:app|router|server)\.(get|post|put|patch|delete|all|use)\(\s*['\"`](/[^'\"`]*)['\"`]")
_DJANGO = c.lazy(r"\b(?:re_)?path\(\s*r?['\"]\^?([^'\"$]*)\$?['\"]")
_JSX_ROUTE = c.lazy(r"<Route\b[^>]*?\bpath=['\"{`]+([^'\"}`]+)")
_HASH = c.lazy(r"""['"`](#/[\w-]+(?:/[\w-]+)*)/?['"`]""")  # a whole literal: "#/watch", never "#/tv/${id}"
_PREFIX = c.lazy(r"\bAPIRouter\([^)]*prefix\s*=\s*['\"]([^'\"]+)['\"]|\bBlueprint\([^)]*url_prefix\s*=\s*['\"]([^'\"]+)")
_ARGPARSE = c.lazy(r"\badd_parser\(\s*['\"]([\w-]+)['\"]")
_CLICK = c.lazy(r"@[\w.]+\.command\(\s*(?:name\s*=\s*)?['\"]([\w-]+)['\"]")
_PAGES_DIR = c.lazy(r"(?:^|/)(?:src/)?pages/(.+)\.(?:jsx?|tsx?|mdx?|vue|astro|svelte)$")  # Next, Nuxt, Astro
_PAGE_DIR = c.lazy(r"(?:^|/)(?:src/)?(?:app|routes)/((?:[^/]+/)*)\+?page\.(?:jsx?|tsx?|mdx?|svelte)$")  # Next app/, SvelteKit
_FASTAPI = c.lazy(r"(?m)^(\w+)\s*=\s*FastAPI\(")
_FLASK = c.lazy(r"(?m)^(\w+)\s*=\s*Flask\(")
_SERVICE = c.lazy(r"(?i)\b(sqlite3?|postgres(?:ql)?|psycopg2?|mysql|mariadb|redis|mongo(?:db)?|kafka|rabbitmq|"
                  r"elasticsearch|boto3|celery|ollama|openai|anthropic)\b")
_COMPOSE = c.lazy(r"(?m)^services:\s*\n((?:[ \t]+.*\n|\s*\n)*)")


def repo_root(start=None):
    """The git checkout around start (the working folder by default), else start itself."""
    start = os.path.abspath(start or os.getcwd())
    top = c._git(start, "rev-parse", "--show-toplevel").strip()
    return top if top and os.path.isdir(top) else start


def _files(root):
    """The repo's files (tracked and new, never ignored), relative; a walk when it isn't a git checkout."""
    out = c._git(root, "ls-files", "--cached", "--others", "--exclude-standard", timeout=20).splitlines()
    if out:
        return out[:50_000]
    found = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "node_modules")
        found += [os.path.relpath(os.path.join(d, n), root) for n in sorted(names)]
        if len(found) > 50_000:
            break
    return found


def _read(root, rel, cap=MAX_READ):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except OSError:
        return ""


def _add(seen, out, item):
    if item and item not in seen:
        seen.add(item)
        out.append(item)


def scan(root):
    """{pages, api, cli, entry, services, surfaces, start}: what a README's figures can show, read from the source."""
    from sampo import map as smap
    from sampo import mission
    files = _files(root)
    pages, api, cli_cmds, seen = [], [], [], set()
    services, apps = [], []
    for rel in files:
        low = rel.lower()
        if not low.endswith(_CODE_EXT) or _SKIP_DIR.search(low) or c.TESTISH.search(rel) or ".min." in low:
            continue
        page = _page_file(rel)
        if page:
            _add(seen, pages, page)
        if os.path.basename(low) == "index.html":
            _add(seen, pages, "/")  # a static page or an app's shell: served at the start page
        text = _read(root, rel)
        if not text:
            continue
        py = low.endswith(".py")
        pre = _PREFIX.search(text) if py else None
        prefix = (pre.group(1) or pre.group(2)) if pre else ""
        for owner, method, path, rest in _PY_ROUTE.findall(text) if py else ():
            listed = re.search(r"methods\s*=\s*[\[(]([^\])]*)", rest)
            gets = "GET" in listed.group(1).upper() if listed else method in ("get", "route", "api_route")
            _sort(((prefix if owner != "app" else "") + path) or "/", gets, pages, api, seen)
        for method, path in _JS_ROUTE.findall(text) if not py else ():  # @router.get("/x") reads as JS too
            if method != "use":
                _sort(path, method in ("get", "all"), pages, api, seen)
        if os.path.basename(low) == "urls.py":
            for path in _DJANGO.findall(text):
                _sort("/" + path.lstrip("/"), True, pages, api, seen)
        for path in _JSX_ROUTE.findall(text):
            _sort(path if path.startswith("/") else "/" + path, True, pages, api, seen)
        for path in _HASH.findall(text):
            _add(seen, pages, path)
        for name in _ARGPARSE.findall(text) + _CLICK.findall(text):
            _add(seen, cli_cmds, name)
        if py:
            for rx, kind in ((_FASTAPI, "fastapi"), (_FLASK, "flask")):
                m = rx.search(text)
                if m:
                    apps.append((kind, rel, m.group(1)))
    manifests = {}
    for rel in files:
        base = os.path.basename(rel)
        if base in ("package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod", "Dockerfile",
                    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "manage.py") \
                and not _SKIP_DIR.search(rel.lower()):
            manifests.setdefault(base, rel)
    for base in ("package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod"):
        text = _read(root, manifests[base]) if base in manifests else ""
        for m in _SERVICE.finditer(text):
            _add(seen, services, m.group(1).lower())
        if base == "package.json" and text:
            try:
                bins = json.loads(text).get("bin")
            except ValueError:
                bins = None
            for name in (bins if isinstance(bins, dict) else {}):
                _add(seen, cli_cmds, name)
        if base == "pyproject.toml":
            block = re.search(r"(?ms)^\[project\.scripts\]\s*\n(.*?)(?=^\[|\Z)", text)
            for name in re.findall(r"(?m)^([\w-]+)\s*=", block.group(1) if block else ""):
                _add(seen, cli_cmds, name)
    for base in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        m = _COMPOSE.search(_read(root, manifests[base])) if base in manifests else None
        for name in re.findall(r"(?m)^  ([\w.-]+):\s*$", m.group(1) if m else ""):
            _add(seen, services, name)
    # the start page first, then paths before hash routes, top-level sections before deep ones, a search page last
    pages.sort(key=lambda r: (r != "/", r.startswith("#"), r.count("/"), "search" in r))
    return {"pages": pages[:200], "api": api[:500], "cli": cli_cmds[:200],  # ponytail: caps against a runaway scan
            "entry": [f for f in files if smap._ENTRY.search(f) and c.CODE.search(f) and not f.endswith(".html")
                      and not _SKIP_DIR.search(f.lower())][:8],
            "services": services[:12], "surfaces": mission.surfaces(root),
            "start": _start(root, files, manifests, apps)}


def _page_file(rel):
    """The route a file-routed page serves (pages/about.tsx, app/about/page.tsx, src/routes/about/+page.svelte), or
    None: an API file, a _app or a [param] route has no screen to shoot."""
    m = _PAGES_DIR.search(rel)
    if m:
        parts = m.group(1).split("/")
        parts = parts[:-1] if parts[-1] == "index" else parts
    else:
        m = _PAGE_DIR.search(rel)
        parts = [x for x in (m.group(1).split("/") if m else []) if x and not (x[0] == "(" and x[-1] == ")")]
    if not m or parts[:1] == ["api"] or any(x.startswith("_") or "[" in x for x in parts):
        return None
    return "/" + "/".join(parts)


def _sort(path, page, pages, api, seen):
    """A route to pages (a GET screen) or api (an API route, or a GET with a path parameter: no value to shoot)."""
    if re.search(r"(^|/)api(/|$)|\.(json|xml|txt)$", path) or not page:
        _add(seen, api, path)
    elif not re.search(r"[{<:*\[]", path):
        _add(seen, pages, path)


def _start(root, files, manifests, apps):
    """A command that starts the app locally, or None: package.json's dev or start script, a FastAPI or Flask app,
    Django's runserver, or a static index.html at the root."""
    pkg = _read(root, manifests["package.json"]) if "package.json" in manifests else ""
    try:
        scripts = json.loads(pkg).get("scripts") or {} if pkg else {}
    except ValueError:
        scripts = {}
    if isinstance(scripts, dict) and ("dev" in scripts or "start" in scripts):
        where = os.path.dirname(manifests["package.json"])
        tool = "pnpm" if "pnpm-lock.yaml" in files else "yarn" if "yarn.lock" in files else "npm"
        run = f"{tool} run dev" if "dev" in scripts else f"{tool} start"
        return f"cd {where} && {run}" if where else run
    for kind, rel, var in apps:
        mod = os.path.splitext(rel)[0]
        if kind == "fastapi":
            d = os.path.dirname(rel)
            return f"uvicorn {'--app-dir ' + d + ' ' if d else ''}{os.path.basename(mod)}:{var} --port 8000"
        return f"python3 -m flask --app {rel} run --port 8000"
    if "manage.py" in manifests:
        return f"python3 {manifests['manage.py']} runserver 127.0.0.1:8000"
    if "index.html" in files:
        return "python3 -m http.server 8000 --bind 127.0.0.1"
    return None


def figures(found, name):
    """[{kind, what, where}]: the product first, then how it works, then what it costs."""
    out, pages = [], found["pages"]
    if pages:
        out.append({"kind": "screenshot", "what": f"the start page ({pages[0]}), desktop and phone, dark and light",
                    "where": "top, under the title: what the product looks like"})
        out += [{"kind": "screenshot", "what": f"the {p} screen, desktop", "where": "Features, beside its section"}
                for p in pages[1:4]]
    elif found["cli"]:
        out.append({"kind": "terminal", "what": f"a real run of {name} {found['cli'][0]} and what it prints",
                    "where": "top, under the title: what using it looks like"})
    parts = [x for x in (c.joinmore(found["entry"], 3),
                         f"{len(found['api'])} API routes" if found["api"] else "",
                         f"commands {c.joinmore(found['cli'], 4)}" if found["cli"] else "",
                         f"services {c.joinmore(found['services'], 5)}" if found["services"] else "") if x]
    out.append({"kind": "diagram", "what": "architecture: " + ("; ".join(parts) or "the main parts and what talks to "
                                                                                    "what"),
                "where": "How it works"})
    if pages and found["api"]:
        flow = f"a request's path: {pages[0]} → {found['api'][0]} → its work → what it stores"
    elif found["cli"]:
        flow = f"a command's path: {found['cli'][0]} → its steps → what it writes"
    else:
        flow = "the main flow, input to output"
    out.append({"kind": "diagram", "what": "flow: " + flow, "where": "How it works, under the architecture"})
    out.append({"kind": "chart", "what": "what it costs: install size, dependencies, time or memory of a real run "
                                         "(measured, never estimated)", "where": "Requirements or Performance"})
    return out


def plan(root):
    found = scan(root)
    name = os.path.basename(root.rstrip("/"))
    routes = ",".join(found["pages"][:6]) or "/"  # not a list: --routes' value; plan_text lists every page
    cmd = found["start"] or "<the command that starts it>"
    nxt = [f'sampo readme shots --cmd "{cmd}" --routes {routes}' if found["pages"] or found["start"] else
           "sampo readme shots --url <where it runs> --routes /",
           f"sampo readme diagram {DIAGRAMS}/architecture.json  (write the spec: nodes, edges, groups)",
           "sampo readme check"]
    return dict(found, name=name, figures=figures(found, name), next=nxt)


def plan_text(got):
    head = (f"sampo readme plan: {got['name']}" + (f" ({', '.join(got['surfaces'])})" if got["surfaces"] else "")
            + f": {len(got['pages'])} page routes, {len(got['api'])} API routes, {len(got['cli'])} commands")
    lines = [head]
    if got["pages"]:
        lines.append("Pages: " + c.joinmore(got["pages"], MAX_ROUTES, " ", "--json"))
    lines.append("Figures (kind · what it shows · where in the README):")
    lines += [f"  {i}. {f['kind']} · {f['what']} · {f['where']}" for i, f in enumerate(got["figures"], 1)]
    lines.append("Next:")
    lines += [f"  {x}" for x in got["next"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- shots

_URL = c.lazy(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])(?::\d{2,5})?[^\s'\"<>)]*")
_ANSI = c.lazy(r"\x1b\[[0-9;?]*[A-Za-z]")
TEXT_JS = ("[document.title, document.body ? document.body.innerText : '', Array.from(document.querySelectorAll("
           "'input,textarea')).map(e => e.value).join('\\n')].join('\\n')")


class App:
    """The app --cmd starts: its own process group, its output kept (the last 64 KB) to find its URL and say why it
    stopped; stop() ends the whole group."""

    def __init__(self, cmd, root):
        env = dict(os.environ, PYTHONUNBUFFERED="1", NO_COLOR="1", FORCE_COLOR="0", BROWSER="none")
        # shell: --cmd is the user's own command line, as they would type it (cd web && npm run dev)
        self.proc = subprocess.Popen(cmd, shell=True, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True, env=env)
        self.out = ""
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in iter(self.proc.stdout.readline, b""):
            self.out = (self.out + _ANSI.sub("", line.decode("utf-8", "replace")))[-65536:]

    def url(self):
        m = _URL.search(self.out)
        return m and re.sub(r"//(?:0\.0\.0\.0|\[::\])", "//127.0.0.1", m.group(0))

    def last(self):
        return c.redact(" / ".join(x.strip() for x in self.out.strip().splitlines()[-4:]))[:400]

    def stop(self):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                self.proc.wait(5)
                break
            except subprocess.TimeoutExpired:
                continue
        self.reader.join(2)


def _answers(url):
    try:
        urllib.request.urlopen(url, timeout=2).close()
        return True
    except urllib.error.HTTPError:
        return True  # it answered: up, whatever it said
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ready(app, url, timeout):
    """The app's URL once it answers: --url, else the first local URL it prints. RuntimeError when it exits or the
    time runs out."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        at = url or (app and app.url())
        if at and _answers(at):
            return at
        if app and app.proc.poll() is not None:
            raise RuntimeError(f"the app exited ({app.proc.returncode}) before it answered: {app.last() or 'no output'}")
        time.sleep(0.3)
    raise RuntimeError(f"nothing answered at {url or (app and app.url()) or 'a printed local URL'} in {timeout} s"
                       + (f"; its output: {app.last()}" if app and app.last() else ""))


def slug(route, taken):
    raw = urllib.parse.urlsplit(route)
    path = (raw.fragment if route.startswith("#") else raw.path).strip("/")
    s = re.sub(r"[^a-z0-9]+", "-", os.path.splitext(path)[0].lower()).strip("-")[:40] or "home"
    name, i = s, 1
    while name in taken:
        i += 1
        name = f"{s}-{i}"
    taken.add(name)
    return name


def target(base, route):
    if re.match(r"(?i)https?://", route):
        return route
    if route.startswith("#"):
        return base.split("#")[0] + route
    return urllib.parse.urljoin(base, route)


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(body, zlib.crc32(kind)))


def optimise(data):
    """The smallest of Chromium's PNG, an optimiser's (oxipng or optipng when one is on PATH; nothing is installed)
    and an opaque RGB re-encode at zlib 9. ponytail: no filters in the re-encode; oxipng does better when present."""
    import tempfile
    from sampo import img as simg
    best = data
    try:
        im = simg.decode(data)
        n, row = im.width * im.height, im.width * 3
        if im.rgba[3::4] == b"\xff" * n:  # a screenshot is opaque: three channels, not four
            rgb = bytearray(n * 3)
            for ch in range(3):
                rgb[ch::3] = im.rgba[ch::4]
            raw = b"".join(b"\0" + rgb[y * row:(y + 1) * row] for y in range(im.height))
            ihdr = struct.pack(">IIBBBBB", im.width, im.height, 8, 2, 0, 0, 0)
            alt = data[:8] + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")
            best = min(best, alt, key=len)
    except ValueError:
        pass
    tool = shutil.which("oxipng") or shutil.which("optipng")
    if tool:
        with tempfile.TemporaryDirectory(prefix="sampo-readme-") as d:
            p = os.path.join(d, "s.png")
            with open(p, "wb") as f:
                f.write(best)
            args = [tool, "-o", "2", "--strip", "safe", p] if "oxipng" in tool else [tool, "-quiet", "-o2", p]
            try:
                subprocess.run(args, capture_output=True, timeout=120, check=True)
                with open(p, "rb") as f:
                    best = min(best, f.read(), key=len)
            except (OSError, subprocess.SubprocessError):
                pass
    return best


def _text(b):
    got = b.call("Runtime.evaluate", b.sid, expression=TEXT_JS, returnByValue=True)
    return str((got.get("result") or {}).get("value") or "")


def _wait_for(b, text, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if text in _text(b):
            return True
        time.sleep(0.25)
    return False


def picture(rel_dark, rel_light, alt, width=None):
    w = f' width="{width}"' if width else ""
    alt = html.escape(alt, quote=True)
    if rel_dark is None:
        return f'<img alt="{alt}" src="{rel_light}"{w}>'
    return (f'<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="{rel_dark}">\n'
            f'  <img alt="{alt}" src="{rel_light}"{w}>\n</picture>')


def shots(root, cmd=None, url=None, routes=None, wait_for=None, timeout=READY_S, out=None):
    """{url, shots, snippets, refused, failed, notes}. Each route at each view, light then dark; one file per view when
    the two match (no dark scheme). A route whose text holds a private string is refused, and nothing of it written."""
    from sampo import shot
    out = os.path.abspath(out or os.path.join(root, SCREENS))
    routes = routes or scan(root)["pages"][:6] or ["/"]
    path, why = shot.find_browser("sampo readme shots")
    if not path:
        raise RuntimeError(why)
    res = {"url": None, "shots": [], "snippets": [], "refused": [], "failed": [], "notes": [] if not why else [why]}
    app = App(cmd, root) if cmd else None
    try:
        with shot.killable():
            res["url"] = base = ready(app, url, timeout)
            os.makedirs(out, exist_ok=True)
            b = shot.Browser(path, VIEWS[0][1], 1, reduced=True)
            try:
                taken = set()
                for route in routes:
                    _route(b, base, route, slug(route, taken), out, root, wait_for, res)
            finally:
                b.close()
    finally:
        if app:
            app.stop()
    return res


def _route(b, base, route, name, out, root, wait_for, res):
    """One route at every view, light then dark; written only once every view has passed the privacy scan."""
    from sampo import shot
    at, taken = target(base, route), []
    for view, size in VIEWS:
        pngs, title = {}, ""
        for scheme in ("light", "dark"):
            try:
                b.emulate(size, 1, scheme, reduced=True)
                shot.load(b, "about:blank")  # a fresh load each time: a hash route alone fires no load event
                res["notes"] += [f"{route} {view} {scheme}: {n}" for n in shot.load(b, at)]
                if wait_for and not _wait_for(b, wait_for, WAIT_FOR_S):
                    res["notes"].append(f"{route} {view} {scheme}: {wait_for!r} never showed in {WAIT_FOR_S} s")
                text = _text(b)
                hits = private_hits(text)
                if hits:
                    res["refused"].append({"route": route, "view": view, "scheme": scheme,
                                           "kinds": [k for k, _ in hits], "found": _said(hits)})
                    return  # nothing of the route is written
                title = title or " ".join(text.split("\n", 1)[0].split())[:80]  # TEXT_JS puts document.title first
                pngs[scheme] = b.png()
            except (OSError, RuntimeError, EOFError, ValueError) as e:
                res["failed"].append({"route": route, "error": shot.safe(str(e))[:300]})
                return
        taken.append((view, pngs, title))
    for view, pngs, title in taken:
        one = pngs["light"] == pngs["dark"]  # ponytail: an animated page without a dark scheme keeps two near twins
        names = {None: f"{name}-{view}.png"} if one else {s: f"{name}-{view}-{s}.png" for s in ("light", "dark")}
        for stale in {f"{name}-{view}.png", f"{name}-{view}-light.png", f"{name}-{view}-dark.png"} - set(names.values()):
            if os.path.isfile(os.path.join(out, stale)) and not os.path.islink(os.path.join(out, stale)):
                os.unlink(os.path.join(out, stale))  # an earlier run's other form of this view
        for scheme, fname in names.items():
            dest = os.path.join(out, fname)
            c.write_atomic(dest, optimise(pngs[scheme or "light"]))
            res["shots"].append(dest)
        rel = lambda f: os.path.relpath(os.path.join(out, f), root).replace(os.sep, "/")  # noqa: E731
        res["snippets"].append(picture(None if one else rel(names["dark"]), rel(names[None] if one else names["light"]),
                                       f"{title or name}: {route}, {view}", None if view == "desktop" else 300))


# ---------------------------------------------------------------- diagram

FONT = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
GLYPH = 0.62  # a monospace glyph's advance, of its size (Menlo .602, DejaVu .602, Consolas .55): wide errs safe
LABEL, SUB, HEAD, TITLE, EDGE = 13, 11, 11, 16, 10
PX, PY, DOT = 12, 9, 22  # a node's padding, and the room its dot takes on the left
GAP_IN, GAP_RANK, GAP_STACK = 52, 92, 26
PAD, HEADER, MARGIN = 16, 36, 28


def _w(text, size, track=0.0):
    return len(text) * (size * GLYPH + track)


def _n(v):
    return format(round(v, 1), "g")


def _cut(text, n=36):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def load_spec(path):
    """The spec, checked: ValueError names the first problem."""
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"can't read {path}: {e}") from None
    if not isinstance(spec, dict):
        raise ValueError("the spec is a JSON object: {nodes, edges, groups, title, direction}")
    nodes, edges, groups = spec.get("nodes"), spec.get("edges", []), spec.get("groups", [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise ValueError(f"nodes: a list of 1 to {MAX_NODES} {{id, label, sub, group}}")
    if not isinstance(edges, list) or len(edges) > MAX_EDGES or not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise ValueError(f"edges: a list of up to {MAX_EDGES} {{from, to, label}}; groups: up to {MAX_GROUPS} "
                         "{id, label, sub}")
    ids, gids = set(), set()
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str) or not g["id"] or g["id"] in gids:
            raise ValueError(f"group {str(g)[:60]}: each needs its own string id")
        gids.add(g["id"])
    for n in nodes:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str) or not n["id"] or n["id"] in ids:
            raise ValueError(f"node {str(n)[:60]}: each needs its own string id")
        if n.get("group") is not None and n["group"] not in gids:
            raise ValueError(f"node {n['id']}: no group {n['group']!r}")
        ids.add(n["id"])
    for e in edges:
        if not isinstance(e, dict) or e.get("from") not in ids or e.get("to") not in ids:
            bad = [str(e.get(k)) for k in ("from", "to") if isinstance(e, dict) and e.get(k) not in ids]
            raise ValueError(f"edge {str(e)[:80]}: no node {' or '.join(bad) or '(from and to)'}")
    if spec.get("direction", "LR") not in ("LR", "TB"):
        raise ValueError("direction: LR (left to right, the default) or TB (top to bottom)")
    return spec


def _ranks(items, edges):
    """{item: rank}: the longest path to it, back edges (found depth-first in the items' order) left out."""
    out_edges = {i: [] for i in items}
    for a, b in edges:
        if a != b and b not in out_edges[a]:
            out_edges[a].append(b)
    state, back = {}, set()

    def visit(i):
        state[i] = 1
        for j in out_edges[i]:
            if state.get(j) == 1:
                back.add((i, j))
            elif j not in state:
                visit(j)
        state[i] = 2
    for i in items:
        if i not in state:
            visit(i)
    rank = {i: 0 for i in items}
    for _ in range(len(items)):  # relax: at most n rounds on a DAG
        moved = False
        for a in items:
            for b in out_edges[a]:
                if (a, b) not in back and rank[b] < rank[a] + 1:
                    rank[b], moved = rank[a] + 1, True
        if not moved:
            break
    return rank


def _mix(a, b, t):
    from sampo import design
    x, y = design.parse_colour(a), design.parse_colour(b)
    return design.hex_of(tuple(p + (q - p) * t for p, q in zip(x, y)))


def _readable(fg, bgs, toward, need=4.6):
    """fg moved toward `toward` until it reads at `need`:1 on each of bgs."""
    from sampo import design
    for i in range(21):
        col = _mix(fg, toward, i / 20)
        if all(design.contrast(design.parse_colour(col), design.parse_colour(b)) >= need for b in bgs):
            return col
    return toward


def palette(root):
    """{theme: {bg, panel, line, ink, muted, accents}, "source"}: DESIGN.md's ## Tokens (or a tokens.json), else the
    repo's CSS custom properties, read by sampo design and mapped to roles by their names; a role the tokens lack is
    mixed from bg and text; no bg and text: the Board look."""
    from sampo import design
    tokens, source = None, "the Board look"
    for rel in ("DESIGN.md", os.path.join("docs", "DESIGN.md"), "tokens.json", "design-tokens.json"):
        if os.path.isfile(os.path.join(root, rel)):
            try:
                tokens, source = design.read_design(os.path.join(root, rel)), rel
                break
            except (ValueError, OSError):
                continue
    if tokens is None:
        css = [os.path.join(root, f) for f in _files(root) if f.endswith(".css") and ".min." not in f
               and not _SKIP_DIR.search(f.lower())][:20]
        if css:
            try:
                rows = design.extract(css)[0]
                tokens, source = {r["name"]: r for r in rows if r["kind"] == "colour"}, "the repo's CSS custom properties"
            except (ValueError, OSError):
                tokens = None
    out = {"source": source}
    for theme in ("dark", "light"):
        roles = {k: h for k, (_, h) in design.token_roles(tokens, theme == "dark").items()} if tokens else {}
        if "bg" not in roles or "text" not in roles:
            out[theme], out["source"] = dict(BOARD[theme]), "the Board look"
            continue
        bg, ink = roles["bg"], roles["text"]
        panel = roles.get("raised") or _mix(bg, ink, 0.04)
        out[theme] = {"bg": bg, "panel": panel, "line": roles.get("border") or _mix(bg, ink, 0.18), "ink": ink,
                      "muted": _readable(roles.get("muted") or _mix(ink, bg, 0.42), (bg, panel), ink),
                      "accents": list(dict.fromkeys(h for h in (roles.get(k) for k in ("accent", "ok", "warn", "bad"))
                                                    if h)) or [_mix(ink, bg, 0.3)]}
    if out["source"] == "the Board look":
        return {"source": out["source"], "dark": dict(BOARD["dark"]), "light": dict(BOARD["light"])}
    if out["dark"] == out["light"]:  # tokens for one scheme only: the other is the Board's neutrals with these accents
        mine = "dark" if design._luminance(design.parse_colour(out["dark"]["bg"])) < 0.2 else "light"
        other = "light" if mine == "dark" else "dark"
        out[other] = dict(BOARD[other], accents=out[mine]["accents"])
    for theme in ("dark", "light"):  # every stroke and badge at 3:1 at least on its ground (WCAG non-text contrast)
        t = out[theme]
        t["accents"] = [_readable(a, (t["bg"], t["panel"]), t["ink"], 3.0) for a in t["accents"]]
    return out


def layout(spec):
    """Every box and edge in screen pixels: {width, height, panels, nodes, edges, title}. Groups are numbered panels
    ranked as units by the edges between them; inside a panel, its nodes by their own edges; one barycentre sweep
    orders each rank. LR runs ranks left to right, TB top to bottom."""
    lr = spec.get("direction", "LR") == "LR"
    swap = (lambda a, b: (a, b)) if lr else (lambda a, b: (b, a))  # (main, cross) <-> (x, y)
    groups = {g["id"]: g for g in spec.get("groups", [])}
    nodes = {n["id"]: dict(n, label=_cut(n.get("label") or n["id"]), sub=_cut(n.get("sub") or "", 44)) for n in spec["nodes"]}
    edges = [(e["from"], e["to"]) for e in spec.get("edges", [])]
    unit_of = {i: f"g:{n['group']}" if n.get("group") else f"n:{i}" for i, n in nodes.items()}
    units = list(dict.fromkeys(unit_of[i] for i in nodes))
    for i, n in nodes.items():  # a node's own size
        n["w"] = max(_w(n["label"], LABEL), _w(n["sub"], SUB)) + DOT + 2 * PX
        n["h"] = 2 * PY + LABEL + (SUB + 5 if n["sub"] else 0)
    inner = {}
    for u in units:  # each unit's nodes laid out by their own edges, in (main, cross) inside its content box
        members = [i for i in nodes if unit_of[i] == u]
        r = _ranks(members, [(a, b) for a, b in edges if unit_of[a] == unit_of[b] == u])
        cols = [[i for i in members if r[i] == k] for k in range(max(r.values()) + 1)]
        main_sz = [max(swap(nodes[i]["w"], nodes[i]["h"])[0] for i in col) for col in cols]
        cross_sz = [sum(swap(nodes[i]["w"], nodes[i]["h"])[1] for i in col) + 14 * (len(col) - 1) for col in cols]
        total_c = max(cross_sz)
        m = 0
        for col, msz, csz in zip(cols, main_sz, cross_sz):
            cpos = (total_c - csz) / 2
            for i in col:
                if lr:  # a column's boxes share its width: edges and labels line up
                    nodes[i]["w"] = msz
                nm, nc = swap(nodes[i]["w"], nodes[i]["h"])
                inner[i] = (m + (msz - nm) / 2, cpos)
                cpos += nc + 14
            m += msz + GAP_IN
        cw, ch = swap(m - GAP_IN, total_c)  # content box in screen px
        if u.startswith("g:"):
            g = groups[u[2:]]
            head = _w(f"{len(units):02d}", HEAD) + 22 + _w(_cut(g.get("label") or g["id"], 28).upper(), HEAD, 0.9) + \
                (_w(" · " + _cut(g.get("sub") or "", 40), HEAD) if g.get("sub") else 0) + 2 * PAD
            pw, ph = max(cw + 2 * PAD, head), ch + HEADER + PAD
            off = ((pw - cw) / 2, HEADER)
        else:
            pw, ph, off = cw, ch, (0, 0)
        inner[u] = {"w": pw, "h": ph, "off": off}
    rank = _ranks(units, [(unit_of[a], unit_of[b]) for a, b in edges if unit_of[a] != unit_of[b]])
    ranks = [[u for u in units if rank[u] == k] for k in range(max(rank.values()) + 1)]
    size = {u: swap(inner[u]["w"], inner[u]["h"]) for u in units}
    totals = [sum(size[u][1] for u in col) + GAP_STACK * (len(col) - 1) for col in ranks]
    widest = max(totals)
    title_h = (TITLE + 22) if spec.get("title") else 0
    pos, m, centre = {}, 0, {}
    preds = {u: [unit_of[a] for a, b in edges if unit_of[b] == u and unit_of[a] != u] for u in units}
    for k, col in enumerate(ranks):
        if k:  # one barycentre sweep: a unit sits near the units that feed it
            col.sort(key=lambda u: (sum(centre[p] for p in preds[u] if p in centre) / max(1, sum(p in centre for p in
                                    preds[u])) if any(p in centre for p in preds[u]) else float("inf"), units.index(u)))
        cpos = (widest - totals[k]) / 2
        msz = max(size[u][0] for u in col)
        for u in col:
            pos[u] = (m + (msz - size[u][0]) / 2, cpos)
            centre[u] = cpos + size[u][1] / 2
            cpos += size[u][1] + GAP_STACK
        m += msz + GAP_RANK
    ox, oy = MARGIN, MARGIN + title_h
    panels, boxes = [], {}
    for n_, u in enumerate(units, 1):
        x, y = swap(*pos[u])
        x, y = x + ox, y + oy
        if u.startswith("g:"):
            g = groups[u[2:]]
            panels.append({"n": n_, "x": x, "y": y, "w": inner[u]["w"], "h": inner[u]["h"], "accent": n_ - 1,
                           "label": _cut(g.get("label") or g["id"], 28), "sub": _cut(g.get("sub") or "", 40)})
        for i in (i for i in nodes if unit_of[i] == u):
            ix, iy = swap(*inner[i])
            boxes[i] = {"x": x + inner[u]["off"][0] + ix, "y": y + inner[u]["off"][1] + iy, "w": nodes[i]["w"],
                        "h": nodes[i]["h"], "label": nodes[i]["label"], "sub": nodes[i]["sub"], "accent": n_ - 1}
    width = max(b["x"] + b["w"] for b in list(boxes.values()) + panels) + MARGIN
    height = max(b["y"] + b["h"] for b in list(boxes.values()) + panels) + MARGIN
    lines = [dict(_route_edge(boxes[e["from"]], boxes[e["to"]], lr), label=_cut(e.get("label") or "", 28),
                  accent=boxes[e["from"]]["accent"]) for e in spec.get("edges", []) if e["from"] != e["to"]]
    for ln in lines:  # a loop swings below (LR) or right of (TB) everything it passes
        height = max(height, ln["reach"][1] + MARGIN)
        width = max(width, ln["reach"][0] + MARGIN)
    if spec.get("title"):
        width = max(width, _w(_cut(spec["title"], 80), TITLE) + 2 * MARGIN)
    return {"width": width, "height": height, "panels": panels, "nodes": boxes, "edges": lines,
            "title": _cut(spec.get("title") or "", 80)}


def _route_edge(a, b, lr):
    """A cubic from a to b: forward along the ranks, across within a rank, or a loop back around."""
    def ab(box):  # (main0, cross0, main1, cross1) of a box
        return (box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]) if lr else \
            (box["y"], box["x"], box["y"] + box["h"], box["x"] + box["w"])
    pt = (lambda m, cr: (m, cr)) if lr else (lambda m, cr: (cr, m))
    am0, ac0, am1, ac1 = ab(a)
    bm0, bc0, bm1, bc1 = ab(b)
    acc, bcc, amc, bmc = (ac0 + ac1) / 2, (bc0 + bc1) / 2, (am0 + am1) / 2, (bm0 + bm1) / 2
    if bm0 >= am1 - 1:  # forward
        p0, p3 = (am1, acc), (bm0, bcc)
        d = max(24, (bm0 - am1) / 2)
        p1, p2 = (am1 + d, acc), (bm0 - d, bcc)
    elif bm0 < am1 and bm1 > am0 and (bc0 >= ac1 or bc1 <= ac0):  # the same rank, beside it: across
        down = bc0 >= ac1
        p0, p3 = (amc, ac1 if down else ac0), (bmc, bc0 if down else bc1)
        d = max(18, abs(p3[1] - p0[1]) / 2) * (1 if down else -1)
        p1, p2 = (amc, p0[1] + d), (bmc, p3[1] - d)
    else:  # back: out of a's underside, a U below both, into b's underside
        low = max(ac1, bc1) + 46
        p0, p3 = (amc, ac1), (bmc, bc1)
        p1, p2 = (amc, low), (bmc, low)
    pts = [pt(*p) for p in (p0, p1, p2, p3)]
    mid = tuple((pts[0][k] + 3 * pts[1][k] + 3 * pts[2][k] + pts[3][k]) / 8 for k in (0, 1))
    reach = (max(p[0] for p in pts), max(p[1] for p in pts))
    return {"pts": pts, "mid": mid, "reach": reach}


def panel(p, pal, nn):
    """One numbered Board panel: the frame a group's nodes sit in. T-3662's calling cards replace this one function."""
    from sampo import panels
    k = panels.kit()
    return k.chrome(k.theme(tokens=pal, scheme=pal.get("scheme", "dark")), p["x"], p["y"], p["w"], p["h"], str(nn), p["label"], "", p["accent"], p["sub"])


def _node(b, pal):
    acc = pal["accents"][b["accent"] % len(pal["accents"])]
    x, y, w, h = b["x"], b["y"], b["w"], b["h"]
    cy = y + PY + LABEL / 2 + 1
    out = [f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" rx="4" fill="{pal["bg"]}" stroke="{acc}" '
           f'stroke-width="1.25"/>',
           f'<circle cx="{_n(x + PX + 4)}" cy="{_n(cy)}" r="7" fill="{acc}" opacity=".18"/>',
           f'<circle cx="{_n(x + PX + 4)}" cy="{_n(cy)}" r="3.5" fill="{acc}"/>',
           f'<text x="{_n(x + PX + DOT)}" y="{_n(y + PY + LABEL - 2)}" font-size="{LABEL}" font-weight="600" '
           f'fill="{pal["ink"]}">{html.escape(b["label"])}</text>']
    if b["sub"]:
        out.append(f'<text x="{_n(x + PX + DOT)}" y="{_n(y + PY + LABEL + SUB + 2)}" font-size="{SUB}" '
                   f'fill="{pal["muted"]}">{html.escape(b["sub"])}</text>')
    return out


def _edge(e, pal, motion=False):
    import math
    acc = pal["accents"][e["accent"] % len(pal["accents"])]
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = e["pts"]
    d = f"M{_n(x0)} {_n(y0)}C{_n(x1)} {_n(y1)} {_n(x2)} {_n(y2)} {_n(x3)} {_n(y3)}"
    dx, dy = x3 - x2, y3 - y2
    ln = math.hypot(dx, dy) or 1
    ux, uy = dx / ln, dy / ln
    tip = [(x3, y3), (x3 - 8 * ux - 4 * uy, y3 - 8 * uy + 4 * ux), (x3 - 8 * ux + 4 * uy, y3 - 8 * uy - 4 * ux)]
    out = [f'<path d="{d}" fill="none" stroke="{acc}" stroke-opacity="{".5" if motion else ".8"}" stroke-width="1.5"/>',
           *([f'<path class="f" d="{d}" fill="none" stroke="{acc}" stroke-width="1.5" stroke-dasharray="3 14"/>']
             if motion else []),
           f'<path d="M{" L".join(f"{_n(px)} {_n(py)}" for px, py in tip)}Z" fill="{acc}"/>']
    if e["label"]:
        mx, my = e["mid"]
        lw = _w(e["label"], EDGE) + 10
        out += [f'<rect x="{_n(mx - lw / 2)}" y="{_n(my - 9)}" width="{_n(lw)}" height="16" rx="3" fill="{pal["bg"]}" '
                f'stroke="{pal["line"]}"/>',
                f'<text x="{_n(mx)}" y="{_n(my + 3)}" font-size="{EDGE}" text-anchor="middle" '
                f'fill="{pal["ink"]}">{html.escape(e["label"])}</text>']
    return out


def svg(lay, pal, title, desc, motion=False):
    """The figure. motion: dashes flowing along each edge (stilled under prefers-reduced-motion). ponytail: off by
    default: a continuously repainted 1280 px image times out works check's render on a loaded machine."""
    w, h = lay["width"], lay["height"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_n(w)} {_n(h)}" width="{_n(w)}" height="{_n(h)}" '
             f'role="img" aria-labelledby="t d" font-family="{FONT}">',
             f'<title id="t">{html.escape(title)}</title><desc id="d">{html.escape(desc)}</desc>',
             *(["<style>.f{animation:f 1.7s linear infinite}@keyframes f{to{stroke-dashoffset:-17}}"
                "@media (prefers-reduced-motion:reduce){.f{animation:none}}</style>"] if motion else []),
             f'<rect width="{_n(w)}" height="{_n(h)}" rx="10" fill="{pal["bg"]}"/>']
    if lay["title"]:
        parts.append(f'<text x="{MARGIN}" y="{MARGIN + TITLE - 2}" font-size="{TITLE}" font-weight="700" '
                     f'fill="{pal["ink"]}">{html.escape(lay["title"])}</text>')
    for p in lay["panels"]:
        parts += panel(p, pal, f"{p['n']:02d}")
    for e in lay["edges"]:
        parts += _edge(e, pal, motion)
    for b in lay["nodes"].values():
        parts += _node(b, pal)
    return "\n".join(parts + ["</svg>"]) + "\n"


def diagram(root, spec_path, out=None):
    """{files, snippet, source, gates}: NAME-dark.svg and NAME-light.svg from the spec, and their works source gates."""
    from sampo import works
    spec = load_spec(spec_path)
    raw = json.dumps(spec, ensure_ascii=False)
    hits = private_hits(raw)
    if hits:
        raise ValueError(f"the spec holds private text: {_said(hits)}")
    name = re.sub(r"[^a-z0-9-]+", "-", str(spec.get("name") or os.path.splitext(os.path.basename(spec_path))[0])
                  .lower()).strip("-") or "diagram"
    out = os.path.abspath(out or os.path.join(root, DIAGRAMS))
    os.makedirs(out, exist_ok=True)
    lay, pal = layout(spec), palette(root)
    nodes = {n["id"]: lay["nodes"][n["id"]]["label"] for n in spec["nodes"]}
    desc = "; ".join(f"{nodes[e['from']]} → {nodes[e['to']]}" + (f" ({e['label']})" if e.get("label") else "")
                     for e in spec.get("edges", [])) or ", ".join(nodes.values())
    title = lay["title"] or name
    files, bad = [], []
    for theme in ("dark", "light"):
        path = os.path.join(out, f"{name}-{theme}.svg")
        text = svg(lay, pal[theme], title, desc, spec.get("motion") is True)
        c.write_atomic(path, text)
        files.append(path)
        bad += [f"{os.path.basename(path)}: {g['gate']} {g['got']} ({g['bar']})"
                for g in works.inspect(text.encode(), "figure")["gates"] if g["ok"] is False]
    rel = lambda p: os.path.relpath(p, root).replace(os.sep, "/")  # noqa: E731
    return {"files": files, "snippet": picture(rel(files[0]), rel(files[1]), title), "source": pal["source"],
            "failed": bad}


# ---------------------------------------------------------------- check

_FENCE = c.lazy(r"(?ms)^(```|~~~).*?^\1[^\n]*$|`[^`\n]+`")
_MD_IMG = c.lazy(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_MD_REF_IMG = c.lazy(r"!\[([^\]]*)\]\[([^\]]*)\]")
_MD_DEF = c.lazy(r"(?m)^\s{0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s|$)")
_PICTURE = c.lazy(r"(?is)<picture\b.*?</picture\s*>")
_TAG = c.lazy(r"(?is)<(img|source)\b([^>]*)>")
_ATTR = c.lazy(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_PAIR = c.lazy(r"(?i)^(.*)-(dark|light)(\.\w+)$")
RASTER = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif")


def _attrs(text):
    return {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ATTR.finditer(text)}


def images(doc):
    """[{src, alt, tag, picture}] in the README: Markdown images (inline and reference), <img> and <source>; code
    blocks and spans are skipped. picture: the index of the <picture> holding it, else None."""
    doc = _FENCE.sub(lambda m: " " * len(m.group(0)), doc)
    out, defs = [], {k.lower(): v for k, v in _MD_DEF.findall(doc)}
    for k, pic in enumerate(_PICTURE.findall(doc)):
        for tag, attrs in _TAG.findall(pic):
            a = _attrs(attrs)
            src = a.get("src") if tag.lower() == "img" else (a.get("srcset") or "").split(",")[0].split()[:1]
            src = src[0] if isinstance(src, list) and src else src if isinstance(src, str) else ""
            out.append({"src": src, "alt": a.get("alt"), "tag": tag.lower(), "picture": k})
        if not any(x["tag"] == "img" and x["picture"] == k for x in out):
            out.append({"src": None, "alt": None, "tag": "picture", "picture": k})
    rest = _PICTURE.sub(" ", doc)
    for tag, attrs in _TAG.findall(rest):
        a = _attrs(attrs)
        if tag.lower() == "img":
            out.append({"src": a.get("src") or "", "alt": a.get("alt"), "tag": "img", "picture": None})
    out += [{"src": src, "alt": alt, "tag": "md", "picture": None} for alt, src in _MD_IMG.findall(rest)]
    out += [{"src": defs.get((ref or alt).lower(), ""), "alt": alt, "tag": "md", "picture": None}
            for alt, ref in _MD_REF_IMG.findall(rest)]
    return out


def check(readme_path):
    """{images, remote, findings: [{file, problem}]} for one README."""
    with open(readme_path, encoding="utf-8", errors="replace") as f:
        doc = f.read()
    base, root = os.path.dirname(os.path.abspath(readme_path)), repo_root(os.path.dirname(os.path.abspath(readme_path)))
    found, seen, local, remote = [], set(), set(), 0
    imgs = images(doc)

    def flag(src, problem):
        if (src, problem) not in seen:
            seen.add((src, problem))
            found.append({"file": src, "problem": problem})
    for im in imgs:
        if im["tag"] == "picture":
            first = next((x["src"] for x in imgs if x["picture"] == im["picture"] and x["src"]), "(an empty picture)")
            flag(first, "a picture with no img fallback")
    for im in imgs:
        src = im["src"]
        if src is None:
            continue
        if im["tag"] in ("md", "img") and not (im["alt"] or "").strip():
            flag(src or "(no src)", "no alt text")
        if not src or re.match(r"(?i)[a-z][\w+.-]*:", src) or src.startswith("//"):
            remote += bool(src)
            continue
        rel = urllib.parse.unquote(src.split("#")[0].split("?")[0])
        path = os.path.normpath(os.path.join(root, rel.lstrip("/")) if rel.startswith("/") else os.path.join(base, rel))
        if not os.path.isfile(path):
            flag(src, "missing")
            continue
        local.add(path)
        ext = os.path.splitext(path)[1].lower()
        size = os.path.getsize(path)
        if ext in RASTER and size > RASTER_KB * 1024:
            flag(src, f"over {RASTER_KB} KB")
        if ext == ".svg":
            if size > SVG_KB * 1024:
                flag(src, f"over {SVG_KB} KB")
            with open(path, encoding="utf-8", errors="replace") as f:
                for kind, _ in private_hits(f.read(2 << 20)):
                    flag(src, f"private text: {kind}")
        m = _PAIR.match(src.split("#")[0].split("?")[0])
        if m:
            other = f"{m.group(1)}-{'light' if m.group(2).lower() == 'dark' else 'dark'}{m.group(3)}"
            if im["picture"] is not None:
                mates = [x["src"] for x in imgs if x["picture"] == im["picture"]]
            else:  # GitHub's other form: ![…](x-dark.png#gh-dark-mode-only) beside ![…](x-light.png#gh-light-mode-only)
                mates = [x["src"].split("#")[0] for x in imgs if x["picture"] is None and x["src"]
                         and "-mode-only" in x["src"]] if "-mode-only" in src else []
            if other not in mates:
                flag(src, f"no {'light' if m.group(2).lower() == 'dark' else 'dark'} pair")
    return {"images": len(local), "remote": remote, "findings": found}


# ---------------------------------------------------------------- the verb

def _shown(path):
    """A path as the terminal shows it: relative to here when it is under here, else whole."""
    rel = os.path.relpath(path)
    return path if rel.startswith("..") else rel


def cmd_readme(args):
    from sampo import cli
    root = os.path.abspath(args.root) if args.root else repo_root()
    if args.action == "plan":
        got = plan(root)
        text = plan_text(got)
        if args.brief is not None:
            p = cli.resolve(args)
            with c.lock(p.dir):
                b = c.find_brief(p, args.brief) if args.brief else c.active_brief_fast(p)
                if not b:
                    raise cli.UsageError(f"--brief: no task {args.brief}" if args.brief else
                                         "--brief: no active task (name one: --brief ID)")
                b.set_section("Figures", "\n".join(text.splitlines()[1:]))
                c.save_brief(p, b)
            text += f"\nWritten into {b.id}'s brief (## Figures)."
        return cli.out(args, got, text)
    if args.action == "shots":
        if not args.cmd and not args.url:
            raise cli.UsageError('sampo readme shots needs --cmd "<the command that starts the app>" or --url URL '
                                 "(sampo readme plan suggests one)")
        routes = [r.strip() for r in (args.routes or "").split(",") if r.strip()] or None
        if routes and len(routes) > MAX_ROUTES:
            raise cli.UsageError(f"--routes takes up to {MAX_ROUTES} routes a run ({len(routes)} given): split them")
        try:
            res = shots(root, args.cmd, args.url, routes, args.wait_for, args.timeout, args.out)
        except RuntimeError as e:
            cli.out(args, {"error": str(e)}, f"✗ sampo readme shots: {e}")
            raise SystemExit(1)
        lines = [f"sampo readme shots: {res['url']} ({'stopped' if args.cmd else 'left running'}): "
                 f"{len(res['shots'])} shots in {_shown(os.path.abspath(args.out or os.path.join(root, SCREENS)))}"]
        lines += [f"  ✗ {r['route']}: refused, the page shows private text: {r['found']}; nothing of it was written "
                  "(change the page's data, or sampo private for a term that is fine to show)" for r in res["refused"]]
        lines += [f"  ✗ {r['route']}: {r['error']}" for r in res["failed"]]
        lines += [f"  note: {n}" for n in res["notes"]]
        if res["snippets"]:
            lines += ["", "Paste into the README (alt text: say what the screen shows):", ""] + res["snippets"]
        cli.out(args, res, "\n".join(lines))
        if res["refused"] or res["failed"]:
            raise SystemExit(1)
        return None
    if args.action == "diagram":
        if not args.target:
            raise cli.UsageError("sampo readme diagram SPEC.json: {title, direction: LR|TB, motion: true|false, groups: [{id, label, sub}], "
                                 "nodes: [{id, label, sub, group}], edges: [{from, to, label}]}")
        try:
            res = diagram(root, os.path.abspath(args.target), args.out)
        except ValueError as e:
            raise cli.UsageError(f"sampo readme diagram: {e}") from None
        text = "\n".join([f"sampo readme diagram: {len(res['files'])} files, drawn in {res['source']}:"]
                         + [f"  {os.path.relpath(f, root)}" for f in res["files"]]
                         + [f"  ✗ {x}" for x in res["failed"]]
                         + ["", "Paste into the README:", "", res["snippet"]])
        cli.out(args, res, text)
        if res["failed"]:
            raise SystemExit(1)
        return None
    path = os.path.abspath(args.target) if args.target else next(
        (os.path.join(root, f) for f in sorted(os.listdir(root)) if re.fullmatch(r"(?i)readme(\.\w+)?", f)), None)
    if not path or not os.path.isfile(path):
        raise cli.UsageError(f"no README here: sampo readme check PATH")
    res = check(path)
    head = (f"sampo readme check {_shown(path)}: {res['images']} images checked"
            + (f", {res['remote']} remote not checked" if res["remote"] else ""))
    cli.out(args, res, "\n".join([head + (": all good." if not res["findings"] else ":")]
                                 + [f"  ✗ {f['file']}: {f['problem']}" for f in res["findings"]]))
    if res["findings"]:
        raise SystemExit(1)
    return None
