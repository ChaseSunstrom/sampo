"""fm find (T-2121): deterministic, language-agnostic finders over a tree. Untested public functions, docs naming code
that doesn't exist, imports no manifest declares, SQL built from a parameter, list membership inside a loop, unseeded
randomness in tests, credentials. Each is a candidate until proven; every finder reports nothing on every clean exam
render (test_find)."""
import bisect
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tokenize
import tomllib

from sampo import core as c

LANG = {".py": "py", ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "ts", ".tsx": "ts", ".mts": "ts",
        ".cts": "ts", ".rb": "rb", ".rs": "rs", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c"}
SKIP = {"node_modules", "vendor", "target", "build", "dist", "__pycache__", "third_party", "site-packages",
        "fixtures", "testdata", "__fixtures__", "__snapshots__", "venv", "env"}  # fixtures are planted on purpose
OPEN_CAP = 5  # T-2235: tasks fm find/prove --capture keep open at once, so repeated runs can't flood the queue
TEST = re.compile(r"(?:^|/)(?:tests?|spec|__tests__)/|(?:^|/)(?:test_[^/]*|conftest\.py)$|[_.](?:test|spec)\.\w+$")
_JSFN = r"^([ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)"
_JSARROW = r"^([ \t]*)(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?\(([^)]*)\)[^=]*=>"
DEFS = {"py": [r"^([ \t]*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)"], "js": [_JSFN, _JSARROW], "ts": [_JSFN, _JSARROW],
        "rb": [r"^([ \t]*)def\s+(?:self\.)?(\w+[?!=]?)\s*\(?([^)\n#]*)"],
        "rs": [r"^([ \t]*)(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)"],
        "c": [r"^()(?:\w+[ \t*]+)+?(\w+)\s*\(([^;{)]*)\)\s*\{?\s*$"]}
DEFS = {k: [re.compile(r) for r in v] for k, v in DEFS.items()}
KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "else", "do", "case", "defined", "elif", "and", "or",
            "not", "in", "unless", "until", "def", "fn", "function", "match", "catch", "typeof", "new", "await", "yield"}
IMPORT = re.compile(r"""^\s*(?:import\b|from\s+\S+\s+import\b|(?:pub\s+)?use\s|extern\s+crate\b|mod\s+\w+\s*;|#\s*include\b|#\[path)"""
                    r"""|\brequire(?:_relative)?\s*\(?\s*["']""")
LOOP = re.compile(r"\b(?:for|while|loop)\b|\.(?:each|forEach|map|filter|times|iter)\b")
SQL = re.compile(r"(?i)\b(?:select\s.*\bfrom|insert\s+into|update\s+\w+\s+set|delete\s+from)\b")
INIT = {"py": r"^\s*(\w+)(?:\s*:[^=]+)?\s*=\s*\[\]", "rb": r"^\s*(\w+)\s*=\s*\[\]",
        "js": r"\b(?:const|let|var)\s+(\w+)(?:\s*:[^=]+)?\s*=\s*\[\]",
        "rs": r"\blet\s+mut\s+(\w+)(?:\s*:[^=]+)?\s*=\s*(?:Vec::new\(\)|vec!\[\])"}
INIT["ts"] = INIT["js"]
USE = {"py": r"\bin\s+{v}\b", "js": r"\b{v}\.(?:includes|indexOf)\(", "rb": r"\b{v}\.(?:include\?|index\b)",
       "rs": r"\b{v}\.(?:contains\(|iter\(\)\.any\()"}
USE["ts"] = USE["js"]
RAND = {"py": r"\brandom\.(?:random|randint|choice|choices|shuffle|uniform|randrange|sample)\(", "js": r"\bMath\.random\(",
        "rb": r"(?<![\w.])rand\(|\.(?:sample|shuffle)\b", "rs": r"\bRandomState::new\(|\bthread_rng\(|\brand::random\b",
        "c": r"(?<!\w)rand\(\s*\)"}
RAND["ts"] = RAND["js"]
SEED = {"py": r"\brandom\.seed\(", "rb": r"\bsrand\(\s*\d|Random\.new\(\s*\d", "c": r"\bsrand\((?!.*\b(?:time|clock|getpid)\s*\()"}
NODE = set("assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns events "
           "fs http http2 https inspector module net os path perf_hooks process punycode querystring readline repl stream "
           "string_decoder test timers tls trace_events tty url util v8 vm wasi worker_threads zlib".split())
RUBY = set("abbrev base64 benchmark bigdecimal bundler cgi coverage csv date delegate digest english erb etc fiddle "
           "fileutils find forwardable getoptlong io ipaddr json logger matrix minitest mkmf monitor net objspace observer "
           "open-uri open3 openssl optparse ostruct pathname pp prettyprint prime pstore psych racc rake rbconfig readline "
           "resolv ripper rubygems securerandom set shellwords singleton socket stringio strscan tempfile test thread time "
           "timeout tmpdir tsort uri weakref yaml zlib".split())
RUST = {"std", "core", "alloc", "crate", "self", "super", "proc_macro", "test"}
CH = set("assert complex ctype errno fenv float inttypes iso646 limits locale math setjmp signal stdalign stdarg stdatomic "
         "stdbool stddef stdint stdio stdlib stdnoreturn string tgmath threads time uchar wchar wctype aio dirent dlfcn "
         "fcntl fnmatch ftw getopt glob grp iconv ifaddrs langinfo libgen malloc mqueue netdb nl_types poll pthread pwd "
         "regex sched search semaphore spawn strings syslog termios unistd utime utmpx wordexp windows winsock2".split())
CDIRS = {"sys", "netinet", "arpa", "net", "linux", "asm", "bits"}
BUILTIN = set("printf fprintf snprintf sprintf puts putchar malloc calloc realloc free strlen strcmp strncmp strcpy strncpy "
              "strcat memcpy memmove memset memcmp abs labs exit abort assert rand srand time clock qsort atoi atol strtol "
              "fopen fclose fread fwrite fgets getchar perror strchr strrchr strstr isalpha isdigit isspace tolower toupper "
              "puts print p pp raise require format sprintf loop lambda proc Integer Float String Array Hash rand "
              "attr_reader attr_accessor include extend private".split())
PY_ALIAS = {"bs4": "beautifulsoup4", "pil": "pillow", "cv2": "opencv", "sklearn": "scikit-learn", "gi": "pygobject",
            # import name (lower case) → distribution, a fixed table: never the host's installed packages (T-2237)
            "yaml": "pyyaml", "dateutil": "python-dateutil", "google": "protobuf", "attr": "attrs", "jwt": "pyjwt",
            "magic": "python-magic", "serial": "pyserial", "crypto": "pycryptodome", "cryptodome": "pycryptodomex",
            "dotenv": "python-dotenv", "git": "gitpython", "markdown": "markdown", "mysqldb": "mysqlclient",
            "psycopg2": "psycopg2-binary", "usb": "pyusb", "zmq": "pyzmq", "skimage": "scikit-image",
            "openssl": "pyopenssl", "docx": "python-docx", "pptx": "python-pptx", "slugify": "python-slugify",
            "jose": "python-jose", "multipart": "python-multipart", "socks": "pysocks", "wx": "wxpython",
            "pkg_resources": "setuptools", "bson": "pymongo", "fitz": "pymupdf", "lxml": "lxml", "ldap": "python-ldap",
            "nacl": "pynacl", "engineio": "python-engineio", "socketio": "python-socketio", "discord": "discord-py",
            "telegram": "python-telegram-bot", "jenkins": "python-jenkins", "consul": "python-consul",
            "snappy": "python-snappy", "levenshtein": "python-levenshtein", "tomli_w": "tomli-w", "ruamel": "ruamel-yaml",
            "yaml_include": "pyyaml-include", "win32api": "pywin32", "win32con": "pywin32", "pythoncom": "pywin32"}


class Src:
    def __init__(self, rel, text):
        self.rel, self.text = rel, text
        self.lines = [x if len(x) <= 2000 else "" for x in text.split("\n")]  # minified or hostile: data, not code
        self.lang, self.test = LANG[os.path.splitext(rel)[1].lower()], bool(TEST.search(rel))
        self.instr, quote = set(), None  # Python lines inside a triple-quoted string: code there is data
        opens = [getattr(tokenize, n, None) for n in ("FSTRING_START", "TSTRING_START")]  # 3.12+ / 3.14+
        closes = [getattr(tokenize, n, None) for n in ("FSTRING_END", "TSTRING_END")]
        try:  # a string token over several lines: its later lines are data (tokenize parses, never runs, the text)
            if self.lang == "py" and any(len(x) > 2000 for x in text.split("\n")):
                raise ValueError("a minified line: tokenizing it costs a token per character")
            starts = []  # rows where the open f/t-strings began, nesting-aware
            for t in tokenize.generate_tokens(io.StringIO(text).readline) if self.lang == "py" else ():
                if t.type in opens:
                    starts.append(t.start[0])
                elif t.type in closes and starts:
                    self.instr.update(range(starts.pop(), t.end[0]))
                elif t.type == tokenize.STRING and t.end[0] > t.start[0]:
                    self.instr.update(range(t.start[0], t.end[0]))  # 1-based rows: start..end-1 are 0-based 2nd..last
        except (tokenize.TokenError, SyntaxError, ValueError):  # unparseable: count the marks instead
            self.instr.clear()
            for i, line in enumerate(self.lines):
                if quote:
                    self.instr.add(i)
                for m in re.finditer(r'"""|\'\'\'', line):
                    quote = m.group() if quote is None else None if m.group() == quote else quote
        self.defs = []  # (line, indent, name, params)
        for i, line in enumerate(self.lines):
            if i in self.instr:
                continue
            for rx in DEFS[self.lang]:
                m = rx.match(line)
                if m and m.group(2) not in KEYWORDS:
                    self.defs.append((i, len(m.group(1)), m.group(2), set(re.findall(r"[A-Za-z_]\w*", m.group(3)))))
                    break
        self._at = [d[0] for d in self.defs]

    def fn(self, i):
        """The definition enclosing line i: the nearest one above. ponytail: top-level code after a function counts as
        that function's; brace-matching would fix it."""
        k = bisect.bisect_right(self._at, i) - 1
        return self.defs[k] if k >= 0 else None

    def body(self, d):
        k = self.defs.index(d)
        return range(d[0], self.defs[k + 1][0] if k + 1 < len(self.defs) else len(self.lines))

    def own(self, d):
        """The words of d's own lines: the def line and the lines indented under it, nested functions included; code
        after it at its own indent or less isn't."""
        return _words("\n".join(self.rows(d)))

    def rows(self, d):
        rows = [self.lines[d[0]]]
        for j in range(d[0] + 1, len(self.lines)):
            row = self.lines[j]
            ind = len(row) - len(row.lstrip())
            if row.strip() and ind <= d[1] and j not in self.instr and not re.match(r"\s*(?:[})\]]|end\b)", row):
                break
            rows.append(row)
        return rows


def _in_str(line, pos):
    """Whether pos sits inside a quoted string on its line (odd count of one quote kind before it)."""
    return any(line[:pos].count(q) % 2 for q in "\"'`")


def _found(finder, kind, s, line, symbol, says):
    return {"finder": finder, "kind": kind, "file": s if isinstance(s, str) else s.rel, "line": line, "symbol": symbol,
            "says": says}


def _word(name):
    return re.compile(rf"(?<!\w){re.escape(name)}(?![\w?!])")


def _words(text):
    """The whole words of a text, as _word sees them: one set lookup per name instead of one scan."""
    return set(re.findall(r"\w+[?!]?", text))


def _git(root, *args, timeout=60):
    """git's stdout as bytes (None on failure); the repo isn't trusted: none of its config may run code (fsmonitor, hooks,
    a signature check's gpg program)."""
    r = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.quotepath=false",
                        "-c", "log.showSignature=false", "-c", "gpg.program=/bin/false", "-c", "gpg.ssh.program=/bin/false",
                        "-c", "gpg.x509.program=/bin/false", "-c", "diff.renameLimit=1000", "-C", root, *args],
                       capture_output=True, timeout=timeout)
    return r.stdout if r.returncode == 0 else None


# a revert of a fix says so in its quoted subject (Revert "Fix x"); a revert of a feature doesn't match
FIX = re.compile(r"\b(fix(es|ed|ing)?|bugs?|bugfix|crash\w*|regress\w*|broken)\b", re.I)


def hotspots(root, n=20):
    """T-2238: [{"file", "fixes"}] best first, or None when git failed: tracked regular files under root by how many of
    the last 500 non-merge commits, those whose subject has a fix word (fix, bug, crash, regress, broken, a revert of
    one), touched them; a renamed file counts under its current name."""
    try:
        ls = _git(root, "ls-files", "-z")
        log = _git(root, "log", "-500", "--no-merges", "--no-show-signature", "-M", "--relative", "-z", "--name-status",
                   "--format=%x01%s")  # --relative: paths as ls-files gives them, from a subdirectory too
    except (OSError, subprocess.SubprocessError):
        return None
    if ls is None or log is None:
        return None
    tracked = {os.fsdecode(p) for p in ls.split(b"\0") if p}
    real, now, fix, count = os.path.realpath(root), {}, False, {}
    toks, i = log.split(b"\0"), 0  # newest first, so a rename's old name maps to the new one for older commits
    while i < len(toks):
        t = toks[i]
        i += 1
        if t.startswith(b"\x01"):  # a commit: its subject
            fix = bool(FIX.search(os.fsdecode(t[1:])))
            continue
        st = t.lstrip(b"\n")  # then a status and one name, or two for a rename or copy
        k = 2 if st[:1] in (b"R", b"C") else 1
        names = [os.fsdecode(x) for x in toks[i:i + k]]
        i += k
        if not st or len(names) < k:
            continue
        new = now.get(names[-1], names[-1])
        if st[:1] == b"R":  # every commit's renames count, fix or not
            now[names[0]] = new
        if fix:
            count[new] = count.get(new, 0) + 1
    # ponytail: only renames inside the 500-commit window and above git's similarity cutoff are followed
    rows = []
    for rel, k in sorted(count.items(), key=lambda x: (-x[1], x[0])):
        p = os.path.join(root, rel)
        try:
            ok = rel in tracked and os.path.realpath(p) == os.path.join(real, *rel.split("/")) and stat.S_ISREG(os.lstat(p).st_mode)
        except OSError:  # tracked but deleted in the work tree
            ok = False
        if ok:
            rows.append({"file": rel, "fixes": k})
    return rows[:n]


def _files(root):
    """([(rel, text)], every path listed): git's view of the tree when it's a checkout, else a walk; vendored
    directories, untracked hidden ones (T-2235: tracked ones like .github are read), generated files, binaries, files
    over 1 MB and anything reached through a symlink are left out."""
    def ls(*how):
        return [os.fsdecode(p) for p in (_git(root, "ls-files", *how, "-z") or b"").split(b"\0") if p]
    try:
        tracked = ls("-c")
        rels = tracked + ls("-o", "--exclude-standard")
    except (OSError, subprocess.SubprocessError):
        tracked, rels = [], []
    tracked, real = set(tracked), os.path.realpath(root)
    if not rels:  # not a checkout, no git, or a folder the parent repo ignores
        for d, dirs, names in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP)
            rels += [os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/") for n in sorted(names)]
    from sampo import map as smap
    out, gen = [], []
    for rel in rels:
        p = os.path.join(root, rel)
        if any((x.startswith(".") and rel not in tracked) or x in SKIP for x in rel.split("/")[:-1]):
            continue
        if os.path.realpath(p) != os.path.join(real, *rel.split("/")):  # T-2235: a symlinked parent directory
            continue
        try:  # regular files only: a symlink may point at /dev/zero, a FIFO or a file outside the tree
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 1 << 20:
                continue
            with open(p, "rb") as f:
                data = f.read((1 << 20) + 1)
        except OSError:
            continue
        if b"\0" not in data[:8192]:
            (gen if smap.is_generated(p) else out).append((rel, data.decode("utf-8", "replace")))
    return out, rels, gen


def _untested(code):
    tests = [s for s in code if s.test or s.lang == "rs" and "#[test]" in s.text]  # Rust unit tests live inline
    if not tests:
        return []
    said = _words("\n".join(line for s in tests for line in s.lines if not IMPORT.search(line)))
    whole = _words("\n".join(s.text for s in tests))
    bodies = {}  # a function a test reaches covers what it calls: black-box (CLI, HTTP) tests reach code by name
    for s in code:
        for d in s.defs:
            if not s.test:
                bodies.setdefault(d[2], set()).update(s.own(d))
    reached, todo = set(), list(said | {"main"})
    while todo:
        n = todo.pop()
        if n not in reached:
            reached.add(n)
            todo += bodies.get(n, ())
    out = []
    for s in code:
        if s.test or s.lang == "c" and not s.rel.endswith((".c", ".cc", ".cpp")):
            continue
        lists = re.findall(r"(?:module\.exports\s*=|export)\s*\{([^}]*)\}", s.text)
        exported = set(re.findall(r"\w+", " ".join(lists))) | set(re.findall(r"exports\.(\w+)\s*=", s.text))
        pub = []
        for i, ind, name, _ in s.defs:
            line = s.lines[i]
            if ind or name == "main" or name.startswith(("test", "_")):
                continue
            if {"py": True, "rb": True, "rs": line.startswith("pub"), "c": not line.startswith(("static", "inline")),
                    "js": line.startswith("export") or name in exported,
                    "ts": line.startswith("export") or name in exported}[s.lang]:
                pub.append((i, name))
        stem = os.path.splitext(os.path.basename(s.rel))[0]
        if pub and stem not in whole | reached and not any(n in reached for _, n in pub):  # lazy("fmx", …) names it too
            out.append(_found("untested", "untested", s, pub[0][0] + 1, stem, f"no test names the module {stem}"))
            continue
        out += [_found("untested", "untested", s, i + 1, name, f"{name} is public and no test calls it")
                for i, name in pub if name not in reached]
    return out


def _docs(files, code, root):
    from sampo import docs
    text = _words("\n".join(s.text for s in code))
    defined = {d[2] for s in code for d in s.defs}
    out = []
    for rel, body in files:
        if not rel.lower().endswith((".md", ".markdown")):
            continue
        body = docs._FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), body)
        spans = {}
        for m in re.finditer(r"`(\w+)\(", body):
            spans.setdefault(m.group(1), body.count("\n", 0, m.start()) + 1)
        if len(set(spans) & defined) * 2 > len(spans):  # the doc is about this code: most of what it names is defined
            out += [_found("docs", "docs", rel, line, n, f"the docs name {n}(), which no code has")
                    for n, line in spans.items() if n not in text]
    try:
        out += [_found("docs", "docs", f["file"], f.get("line") or 0, f["detail"], f"docs {f['kind']}: {f['detail']}")
                for f in docs.scan(root)]
    except (OSError, ValueError):
        pass
    return out


def _sql(s):
    out, seen = [], set()
    for i, line in enumerate(s.lines):
        d = s.fn(i)
        if not d or not SQL.search(line) or not re.search(r"[\"'`]", line):
            continue
        names = set(re.findall(r"\+\s*([A-Za-z_]\w*)\b(?!\s*[.(\[])", line))
        names |= set(re.findall(r"(?<![.\w])([A-Za-z_]\w*)\s*\+\s*[\"'`]", line))
        names |= set(re.findall(r"\{\s*([A-Za-z_]\w*)\s*\}", line))  # f-string, ${…}, #{…}, format!("{x}")
        tail = line[max(line.rfind('"'), line.rfind("'"), line.rfind("`")) + 1:]
        if re.search(r"format|printf", line):
            names |= set(re.findall(r",\s*([A-Za-z_]\w*)\s*(?=[,)])", tail))
        names |= set(re.findall(r"%\s*\(?\s*([A-Za-z_]\w*)\b(?!\s*[.(\[])", tail))
        hit = sorted(names & d[3])
        if hit and d[2] not in seen:
            seen.add(d[2])
            out.append(_found("sql", "vuln", s, i + 1, d[2], f"SQL built from the parameter {hit[0]} (injection)"))
    return out


def _slow(s):
    out = []
    if s.lang == "c":
        for d in s.defs:
            rows = [s.lines[j] for j in s.body(d)]
            ks = set(re.findall(r"\[(\w+)\+\+\]", "\n".join(rows)))
            for j, line in enumerate(rows):
                m = re.search(r"\bfor\s*\([^;]*;\s*\w+\s*<\s*(\w+)\s*;", line)
                if m and m.group(1) in ks and "==" in line + (rows[j + 1] if j + 1 < len(rows) else "") and \
                        any(LOOP.search(r) for r in rows[1:j]):
                    out.append(_found("slow", "slow", s, d[0] + j + 1, d[2], "a linear scan of the output for each "
                                      "input item (quadratic): a hash set"))
                    break
        return out
    for i, line in enumerate(s.lines):
        m, d = re.search(INIT[s.lang], line), s.fn(i)
        if not m or not d:
            continue
        use, looped = re.compile(USE[s.lang].format(v=re.escape(m.group(1)))), False
        for j in range(i + 1, s.body(d).stop):
            row = s.lines[j]
            looped = looped or bool(LOOP.search(row) and any(_word(p).search(row) for p in d[3]))
            u = use.search(row)
            if looped and u and not (s.lang == "py" and re.search(r"\bfor\s+[\w\s,()]+$", row[:u.start()])):
                out.append(_found("slow", "slow", s, j + 1, d[2], f"membership test on the list {m.group(1)} inside a "
                                  "loop over the input (quadratic): a set"))
                break
    return out


def _flaky(s):
    if s.lang not in RAND or s.lang in SEED and re.search(SEED[s.lang], s.text):
        return []
    out, seen = [], set()
    for i, line in enumerate(s.lines):
        m = re.search(RAND[s.lang], line)
        if m and not _in_str(line, m.start()):
            d = s.fn(i)
            name = d[2] if d else ""
            if name not in seen:
                seen.add(name)
                out.append(_found("flaky", "flaky", s, i + 1, name, "unseeded randomness in a test: it passes or "
                                  "fails by chance"))
    return out


def _norm(name):
    return re.sub(r"[-_.]+", "-", name.lower())


def _manifest(b, text):
    """The package names one manifest declares."""
    names = set()
    if re.match(r"(?:requirements|constraints).*\.txt$", b):
        names |= set(re.findall(r"(?m)^\s*([A-Za-z0-9][\w.-]*)", text))
    elif b == "package.json":
        try:
            pkg = json.loads(text)
        except (ValueError, RecursionError):
            return set()
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            names |= set((pkg.get(key) or {}) if isinstance(pkg, dict) else ())
        names.add(str(pkg.get("name") or "") if isinstance(pkg, dict) else "")
    elif b in ("pyproject.toml", "Cargo.toml"):
        t = tomllib.loads(text)
        proj, poetry = t.get("project") or {}, (t.get("tool") or {}).get("poetry") or {}
        reqs = list(proj.get("dependencies") or []) + [x for v in (proj.get("optional-dependencies") or {}).values()
                                                       for x in v] + \
            [x for v in (t.get("dependency-groups") or {}).values() for x in v if isinstance(x, str)]
        names |= {m.group(0) for m in (re.match(r"[A-Za-z0-9][\w.-]*", str(r)) for r in reqs) if m}
        for table in (poetry.get("dependencies"), poetry.get("dev-dependencies"), t.get("dependencies"),
                      t.get("dev-dependencies"), t.get("build-dependencies"), (t.get("workspace") or {}).get("dependencies"),
                      *[g.get("dependencies") for g in (poetry.get("group") or {}).values()]):
            names |= set(table or {})
        names |= {str(proj.get("name") or ""), str(poetry.get("name") or ""), str((t.get("package") or {}).get("name") or "")}
    elif b == "Gemfile" or b.endswith(".gemspec"):
        names |= set(re.findall(r"""(?m)^\s*(?:gem|\w+\.add_(?:runtime_|development_)?dependency)\s*\(?\s*["']([^"']+)""", text))
    return names


def _declared(root, files):
    names = set()
    for rel, text in files:
        try:
            names |= _manifest(os.path.basename(rel), text)
        except (ValueError, TypeError, AttributeError, RecursionError):  # a malformed manifest declares nothing
            pass
    return {_norm(n) for n in names if n}


def _imports(s, local, mods):
    """[(line, module, bindings)] for the third-party-looking imports of a file."""
    out = []
    for i, line in enumerate(s.lines):
        if s.lang == "py":
            m = re.match(r"^(\s*)(?:from\s+([\w.]+)\s+import\s+([^#]+)|import\s+([^#]+))", line)
            if not m or i in s.instr or (m.group(2) or "").startswith("."):
                continue
            if m.group(1):  # nested: an optional (try:) or conditional (if …:) import isn't a hard dependency
                up = next((s.lines[k] for k in range(i - 1, -1, -1) if s.lines[k].strip() and
                           len(s.lines[k]) - len(s.lines[k].lstrip()) < len(m.group(1))), "")
                if re.match(r"\s*(?:try|if|elif|else|except)\b", up):
                    continue
            if m.group(2):
                tops = [(m.group(2).split(".")[0], [x.split()[-1] for x in m.group(3).strip("()\\ ").split(",") if x.strip() not in ("", "*")])]
            else:
                tops = [(x.split()[0].split(".")[0], [x.split()[-1].split(".")[0]]) for x in m.group(4).split(",") if x.strip()]
            out += [(i, t, b) for t, b in tops if t not in sys.stdlib_module_names and t != "__future__" and t not in local]
        elif s.lang in ("js", "ts"):
            for m in re.finditer(r"""(?:\b(?:const|let|var)\s+([^=]+?)\s*=\s*)?\brequire\s*\(\s*["']([^"']+)["']\s*\)|"""
                                 r"""^\s*(?:import|export)\s+(?:(.+?)\s+from\s+)?["']([^"']+)["']""", line):
                spec, clause = m.group(2) or m.group(4), m.group(1) or m.group(3) or ""
                if spec.startswith(("npm:",)):
                    spec = spec[4:]
                if spec.startswith((".", "/", "~", "@/", "#")) or ":" in spec:
                    continue
                name = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]  # not a list: a path or word fragment, not a list
                if name not in NODE and name not in local:
                    out.append((i, name, [w for w in re.findall(r"\w+", clause) if w not in ("as", "type")]))
        elif s.lang == "rb":
            m = re.match(r"""^\s*require\s*\(?\s*["']([^"']+)["']""", line)
            if m and m.group(1).split("/")[0] not in RUBY and os.path.basename(m.group(1)) not in local:
                out.append((i, m.group(1).split("/")[0], []))
        elif s.lang == "rs":
            m = re.match(r"^\s*(?:pub\s+)?(?:use\s+(?:::)?(\w+)(?:::(.*?))?|extern\s+crate\s+(\w+))\s*;", line)
            top = m and (m.group(1) or m.group(3))
            if top and top not in RUST and not top[0].isupper() and top not in local and top not in mods:
                out.append((i, top, [w for w in re.findall(r"\w+", (m.group(2) or top).split("::")[-1]) if w not in ("self", "as")] or [top]))
        elif s.lang == "c":
            m = re.match(r"^\s*#\s*include\s*<([^>]+)>", line)
            h = m and m.group(1)
            if h and h.endswith((".h", ".hpp")) and h[:-2].split(".")[0] not in CH and h.split("/")[0] not in CDIRS:
                out.append((i, h, []))
    return out


def _deps(root, files, code, rels):
    declared = _declared(root, files)
    local = {os.path.splitext(os.path.basename(r))[0] for r in rels} | {p for r in rels for p in r.split("/")[:-1]}
    headers = {os.path.basename(r) for r, _ in files if r.endswith((".h", ".hpp"))}
    build = "\n".join(t for r, t in files if os.path.basename(r) in ("Makefile", "makefile", "CMakeLists.txt",
                                                                    "meson.build", "configure.ac"))
    mods = {m for s in code if s.lang == "rs" for m in re.findall(r"(?m)^\s*(?:pub\s+)?mod\s+(\w+)", s.text)}
    defined = {d[2] for s in code for d in s.defs}
    out = []
    for s in code:
        for i, mod, binds in _imports(s, local, mods):
            n = _norm(mod)
            if s.lang == "c":
                stem = os.path.splitext(os.path.basename(mod))[0]
                if os.path.basename(mod) in headers or re.search(rf"\b{re.escape(stem)}\b", build):
                    continue
            elif s.lang == "py":
                alias = _norm(PY_ALIAS.get(mod.lower(), "")) or None
                cands = {n, alias}
                # ponytail: substring match (yaml in pyyaml) can hide an undeclared import (req vs requests);
                # grow PY_ALIAS if a miss shows up in the exam
                if any(x and (x in declared or len(x) >= 3 and any(x in d for d in declared)) for x in cands):
                    continue
            elif n in declared:
                continue
            if binds:
                users = {s.fn(j)[2] for j, row in enumerate(s.lines) if j != i and s.fn(j) and not IMPORT.search(row)
                         and any(_word(b).search(row) for b in binds)}
            else:  # Ruby and C bind no name: the functions calling something defined nowhere in the repo
                users = {s.fn(j)[2] for j, row in enumerate(s.lines) if s.fn(j) and j != s.fn(j)[0] and any(
                    x not in defined and x not in BUILTIN and x not in KEYWORDS
                    for x in re.findall(r"(?<![\w.:>$@])([A-Za-z_]\w*[?!]?)\s*\(", row))}
            out.append(_found("deps", "dep", s, i + 1, " ".join(sorted(users)) or mod,
                              f"imports {mod}, which no manifest declares and the standard library doesn't have"))
    return out


def scan(root, prec=None, proven=None, home=None, extra=()):
    """(files scanned, findings); ranked best first when prec (each finder's precision, a dict, maybe empty) is given;
    proven and home as in rank (home: what find_key paths are relative to, default root); extra: findings made
    elsewhere (the tool rung's) joined to these."""
    files, rels, gen = _files(root)
    code = [Src(r, t) for r, t in files if os.path.splitext(r)[1].lower() in LANG]
    from sampo import secrets
    known = {r for r, _ in files + gen}  # regular, unlinked files this scan read: a tool's `//line` can name any path
    out = _untested(code) + _docs(files, code, root) + _deps(root, files, code, rels) + [f for f in extra if f["file"] in known]
    for s in code:
        out += _flaky(s) if s.test else _sql(s) + _slow(s)
    for rel, text in files + gen:  # a generated file can still hold a pasted key
        if TEST.search(rel):  # a test's credentials are fakes by design
            continue
        out += [_found("secrets", "secret", rel, n, "", f"credential: {kind}")
                for n, kind in secrets.scan_text(text, config=bool(secrets.CONFIG.search(rel)))]
    out = sorted(out, key=lambda f: (f["file"], f["line"], f["finder"]))
    return len(files), out if prec is None else rank(
        out, dict(files), prec, proven, os.path.realpath(root), home or os.path.realpath(root), {s.rel: s for s in code})


def find(root):
    return scan(root)[1]


SEVERITY = {"secret": 1.0, "vuln": 1.0, "bug": 0.9, "dep": 0.6, "flaky": 0.5, "untested": 0.4, "slow": 0.3, "docs": 0.2}
FLOOR, BAR = 0.3, 0.1  # a finder whose precision is under FLOOR is muted; a score under BAR is held back as noise
HALF_LIFE = 180  # days: an outcome weighs half as much per HALF_LIFE, so a mute lifts once its drops age (7: two weeks)


PROOF = 1.25  # T-2132: fm prove proved this lead and its function is unchanged


def _lead_fn(texts, f, srcs, memo):
    """(hash, line span) of the lead's enclosing function, or of its own line and 0 when it has none; (None, 0) when the
    file's text is unavailable (a generated file). Parsed, never run; srcs and memo cache per file and per function, so
    many leads in one function cost one pass."""
    rel, t = f["file"], texts.get(f["file"])
    if t is None:
        return None, 0
    if rel not in srcs:
        srcs[rel] = Src(rel, t) if os.path.splitext(rel)[1].lower() in LANG else None
    s = srcs[rel]
    d = s and s.fn(f["line"] - 1)
    if d:
        if (rel, d[0]) not in memo:
            rows = s.rows(d)
            while len(rows) > 1 and not rows[-1].strip():  # blank lines between functions aren't the function's
                rows.pop()
            memo[rel, d[0]] = (hashlib.sha1("\n".join(rows).encode("utf-8", "replace")).hexdigest()[:16], len(rows))
        return memo[rel, d[0]]
    lines = memo.setdefault(rel, t.split("\n"))
    if not 0 < f["line"] <= len(lines):
        return None, 0
    return hashlib.sha1(lines[f["line"] - 1].encode("utf-8", "replace")).hexdigest()[:16], 0


def proof_hash(texts, f):
    """What fm prove records for a proven lead: a hash of its own function's source, so an edit elsewhere keeps it; None
    when the file's text is unavailable (nothing to record)."""
    return _lead_fn(texts, f, {}, {})[0]


def rank(found, texts, prec, proven=None, root=".", home=".", srcs=None):
    """Best first, each given score = severity x reach x its finder's precision (default 0.5, the prior) x effort x proof.
    Reach is 1 plus up to 0.5 for the other files naming the symbol, so it never lifts slow or docs past a vuln or a bug.
    Effort is max(0.5, 1 - span/320) of the lead's function (a small fix first; 1 with no function). Proof is PROOF
    when proven (find_key -> proof_hash, from fm prove) still matches, but a proven lead's score stays under 0.98 x
    every unproven lead of a higher severity kind in the run (its own unlifted score, if higher, stands). A finder under
    FLOOR is muted and a score under BAR held back: both sort last, still listed."""
    words = {rel: _words(t) for rel, t in texts.items()}
    rows, srcs, memo = [], {} if srcs is None else srcs, {}
    for key, f in keyed(found, root, home):
        sym = (re.findall(r"\w+[?!]?", f["symbol"]) or [""])[0]
        n = sum(1 for rel, w in words.items() if rel != f["file"] and sym in w) if sym else 0
        reach = 1 + 0.5 * min(n, 10) / 10
        fp = prec.get(f["finder"], 0.5)
        sev = SEVERITY.get(f["kind"], 0.2)
        h, span = _lead_fn(texts, f, srcs, memo)
        base = sev * reach * fp * max(0.5, 1 - span / 320)
        rows.append((f, sev, fp, reach, base, bool(proven) and h is not None and proven.get(key) == h))
    low = {}  # severity -> its lowest unproven score
    for f, sev, fp, reach, base, won in rows:
        if not won:
            low[sev] = min(low.get(sev, base), base)
    out = []
    for f, sev, fp, reach, base, won in rows:
        raw = base
        if won:
            raw = max(base, min([base * PROOF] + [0.98 * v for s, v in low.items() if s > sev]))
        score = round(raw, 3)
        held = "muted" if fp < FLOOR else "below the bar" if score < BAR else None
        out.append({**f, "score": score, "reach": reach} | ({"held": held} if held else {}))
    return sorted(out, key=lambda f: (bool(f.get("held")), -f["score"], f["file"], f["line"]))


PROOFS = "find-proofs.json"  # state: {find_key: proof_hash}, written only by fm prove through write_atomic


def proofs(p):
    try:
        with open(os.path.join(p.dir, PROOFS)) as fh:
            got = json.load(fh)
        return {k: v for k, v in got.items() if isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def record(p, proven):
    """Keep fm prove's proven leads ({find_key: proof_hash}) for rank; the newest 1000 stay."""
    with c.lock(p.dir):
        got = proofs(p)
        for k in proven:
            got.pop(k, None)
        c.write_atomic(os.path.join(p.dir, PROOFS), json.dumps(dict(list((got | proven).items())[-1000:])))


def precision(p):
    """{finder: (real + 5) / (closed + 10)} over the closed tasks tagged finder= (the prior 0.5 weighs as 10 cases): a
    dropped one is a false alarm, a done one real unless fmoutcomes saw it reverted. Open ones say nothing yet. Each
    weighs 0.5 ** (age / HALF_LIFE), so a muted finder is heard again once its drops age instead of staying mute."""
    from sampo import outcomes
    tagged = [b for b in c.load_briefs(p, include_archive=True) if b.meta.get("finder") and b.status in c.CLOSED]
    fate = outcomes.outcomes(p) if any(b.status == "done" for b in tagged) else {}
    real, n = {}, {}
    for b in tagged:
        fd, w = b.meta["finder"], 0.5 ** ((c.age_days(outcomes.done_at(b)) or 0) / HALF_LIFE)
        n[fd] = n.get(fd, 0) + w
        real[fd] = real.get(fd, 0) + w * (b.status == "done" and fate.get(b.id, {}).get("fate") != "reverted")
    return {fd: round((real[fd] + 5) / (k + 10), 3) for fd, k in n.items()}


def keyed(found, root, home):
    """(find_key, lead) pairs: finder, file from home, symbol, message, and which twin when one function says it twice."""
    twins, keys = {}, {}
    for i in sorted(range(len(found)), key=lambda i: (found[i]["file"], found[i]["line"], found[i]["finder"])):
        f = found[i]  # twins number in (file, line, finder) order whatever order found comes in: rank, capture and prove agree
        key = f"{f['finder']}:{os.path.relpath(os.path.join(root, f['file']), home)}:{f['symbol']}"
        key += "" if f["finder"] == "bug-rung" else f":{f['says']}"  # T-2242: a model's wording changes every run
        twins[key] = twins.get(key, 0) + 1
        keys[i] = key + (f"#{twins[key]}" if twins[key] > 1 else "")
    return [(keys[i], f) for i, f in enumerate(found)]


def settled(p):
    """find_keys never filed again: still open, dropped (the refutation ledger) or fixed and then reverted. A fix that
    held and then comes back is a regression, so it may be filed again."""
    from sampo import outcomes
    tagged = [b for b in c.load_briefs(p, include_archive=True) if b.meta.get("find_key")]
    fate = outcomes.outcomes(p) if any(b.status == "done" for b in tagged) else {}
    return {b.meta["find_key"] for b in tagged if b.status != "done" or fate.get(b.id, {}).get("fate") == "reverted"}


def open_finders(p):
    """T-2235: open tasks a finder filed (fm find, fm prove or the scout), which OPEN_CAP bounds."""
    return sum(1 for b in c.load_briefs(p) if b.meta.get("finder") and b.status not in c.CLOSED)


def filed_line(p, made):
    """fm find/prove --capture's last line, saying why nothing was filed when the open cap stopped it."""
    full = not made and open_finders(p) >= OPEN_CAP
    return (f"\nFiled {len(made)} task(s): {', '.join(made)}" if made else
            f"\nFiled 0 task(s): none ({OPEN_CAP} finder tasks open already: finish or drop some)" if full else
            "\nFiled 0 task(s): none new") + "."


def capture(p, found, n, root, proof=None):
    """Up to n new leads not held back, best first, as discovered tasks tagged finder= and keyed (keyed()) so a line
    shift never refiles one, and never a settled() key. A tree outside the project is text it doesn't trust: those
    tasks wait for the user's yes. proof (T-2123): fm prove's proven tickets: each task is planned, its criterion
    proof(key), red until the fix. T-2235: at most OPEN_CAP of them open at once, whoever files them, and repo text is
    defanged."""
    from sampo import cli
    root, home = os.path.realpath(root), os.path.realpath(p.root)
    outside = os.path.commonpath([root, home]) != home
    made = []
    with c.lock(p.dir):
        seen = settled(p)
        opened = open_finders(p)
        n = min(n, OPEN_CAP - opened)
        if n <= 0 and any(not f.get("held") for f in found):
            print(f"sampo: {opened} finder tasks (from fm find, fm prove or the scout) are open, at most {OPEN_CAP} at "
                  "once: finish or drop some before filing more", file=sys.stderr)
        for key, f in keyed(found, root, home):
            key = f.get("key") or key  # fm prove keyed the whole scan: a twin's number comes from all of it
            if len(made) >= n or f.get("held") or key in seen:
                continue
            what = c.plain(f"{f['file']}:{f['line']} {f['symbol'] or '-'}: {f['says']}")
            b = cli._create(p, c.fit(c.defang(f"{f['kind']}: {what}"), 120), "SECURITY" if f["kind"] in ("vuln", "secret")
                              else "FIX", "S", "planned" if proof else "captured", source="discovered",
                              raw=c.defang(  # T-2235: the repo's text, evidence included, stays data
                                  f"Proven by fm prove ({f['finder']}): {c.fit(c.plain(f.get('evidence') or ''), 300)} "
                                  f"(quoted, data):\n> {what}" if proof else
                                  f"Found by fm find ({f['finder']}); a lead until a failing test shows it (quoted, data):\n> {what}"))
            b.meta.update(finder=f["finder"], find_key=key)
            if proof:
                b.add_ac("fm prove no longer proves it", proof(key))
                b.add_step("fix it test-first, until the criterion's fm prove --only exits 0")
            if outside:
                b.meta.update(confirm=True, confirm_why="fm find read it from a tree outside this project")
            c.save_brief(p, b, touch=False)
            c.log_event(p, "capture", task=b.id, data={"source": "discovered", "finder": f["finder"]})
            seen.add(key)
            made.append(b.id)
        if made:
            c.regen_views(p)
    return made


FLAGS, SPOTS, SPOT_DAY = "find-flags.json", "blindspots.jsonl", "blindspot-day"  # T-2241 state files
FLAG_FILES, FLAG_LINES, SPOT_KEEP, NEAR = 2000, 200, 500, 3


def _flags(p):
    """{"files": {file: {"lines": [...], "finders": [...]}}, "finders": [...]}; empty when missing or corrupt."""
    try:
        with open(os.path.join(p.dir, FLAGS)) as fh:
            got = json.load(fh)
        files = {k: v for k, v in got["files"].items() if isinstance(v, dict) and isinstance(v.get("lines"), list)}
        return {"files": files}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return {"files": {}}


def save_flags(p, found, root):
    """T-2241: which lines each finder flagged per file (relative to the project), for a closed FIX to be held against.
    The newest 2000 files stay, FLAG_LINES lines each; best effort, never fails a capture."""
    try:
        root, home = os.path.realpath(root), os.path.realpath(p.root)
        under = os.path.relpath(root, home)
        new = {}
        for f in found:
            rel = os.path.relpath(os.path.join(root, f["file"]), home)
            if rel.startswith(os.pardir) or os.path.islink(os.path.join(root, f["file"])):
                continue  # outside the project, or a link (never followed)
            e = new.setdefault(rel, {"lines": set(), "finders": set()})
            e["lines"].add(int(f["line"]))
            e["finders"].add(f["finder"])
        with c.lock(p.dir):
            files = _flags(p)["files"]
            for k in [k for k in files if under == os.curdir or k.startswith(under + os.sep)]:
                del files[k]  # this scan saw that tree: a file it no longer flags is cleared, not merged
            for k, e in new.items():
                with open(os.path.join(home, k), "rb") as fh:  # the blob hash git would give: line numbers mean nothing once it differs
                    data = fh.read()
                files[k] = {"lines": sorted(e["lines"])[:FLAG_LINES], "finders": sorted(e["finders"]),
                            "blob": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()}
            c.write_atomic(os.path.join(p.dir, FLAGS), json.dumps({"files": dict(list(files.items())[-FLAG_FILES:])}))
    except (OSError, ValueError, KeyError, TypeError, c.LockTimeout):
        pass


def _changed_lines(text):
    """{file: [line, ...]} of the new-side lines in a -U0 diff (a pure deletion names the line it sat before)."""
    out, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith("+++ "):
            cur = ln[6:].rstrip("\t") if ln.startswith("+++ b/") else None  # git adds a TAB after a name with a space
        elif cur and (m := re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", ln)):
            start, n = int(m[1]), int(m[2] or 1)
            out.setdefault(cur, []).extend(range(start, start + max(n, 1)))
    return out


def blindspots(p, b):
    """T-2241: a closed FIX's changed lines (its own files, from its start point) that no saved flag sits within NEAR
    lines of, one record per file in blindspots.jsonl. Best effort: the caller swallows any error. No flags saved yet
    means no finder ever ran here, so nothing is a miss."""
    from sampo import cli
    import time
    flags = _flags(p)["files"]
    if not flags:  # a project that never captured pays nothing
        return []
    end = time.monotonic() + 10  # the hook's whole budget

    def git(*a):
        return _git(p.root, *a, timeout=max(1, end - time.monotonic()))
    base, tree = c.task_base(p.root, b), c.worktree_tree(p.root)
    if not base or not tree:
        return []
    # --relative: paths are p.root's, as the flags keys are, even when p.root is a folder inside the git root
    opts = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--relative")
    files = (git("diff", "--name-only", *opts, base, tree) or b"").decode("utf-8", "replace").splitlines()
    theirs = set(cli._theirs(p, b, files))
    files = [f for f in files if f not in theirs and os.path.splitext(f)[1].lower() in LANG and not TEST.search(f)][:50]
    files = [f for f in files if "blob" not in flags.get(f, {})  # flags for a file that changed since the capture mean nothing
             or (git("rev-parse", f"{base}:./{f}") or b"").decode().strip() == flags[f]["blob"]]
    if not files:
        return []
    diff = git("diff", "-U0", *opts, base, tree, "--", *files)
    recs = []
    for f, lines in _changed_lines((diff or b"").decode("utf-8", "replace")).items():
        near = flags.get(f, {}).get("lines", [])
        miss = sorted({n for n in lines if not any(isinstance(x, int) and abs(x - n) <= NEAR for x in near)})
        if miss:
            recs.append({"task": b.id, "file": f, "lines": miss[:20], "finders": flags.get(f, {}).get("finders", []),
                         "at": c.now()})
    with c.lock(p.dir):  # a reopened task closing again replaces its own records
        old = [r for r in _spots(p) if r["task"] != b.id]
        c.write_atomic(os.path.join(p.dir, SPOTS), "".join(json.dumps(r) + "\n" for r in (old + recs)[-SPOT_KEEP:]))
    return recs


def _spots(p):
    out = []
    try:
        with open(os.path.join(p.dir, SPOTS), encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and all(isinstance(r.get(k), str) for k in ("task", "file")) \
                        and isinstance(r.get("lines"), list):
                    out.append(r)
    except OSError:
        pass
    return out


def cmd_blindspots(p, args):
    """fm find --blindspots: the records, newest last; with --capture, one task a day for the newest one."""
    from sampo import cli
    rows = _spots(p)[-50:]
    made, why = [], ""
    day = c.now()[:10]
    if getattr(args, "capture", None):
        with c.lock(p.dir):
            try:  # {"day": the last filing's, "filed": ["task|file", ...]}
                with open(os.path.join(p.dir, SPOT_DAY)) as fh:
                    st = json.load(fh)
                last, filed = str(st["day"]), [x for x in st["filed"] if isinstance(x, str)]
            except (OSError, ValueError, KeyError, TypeError):
                last, filed = "", []
            todo = [r for r in rows if f"{r['task']}|{r['file']}" not in filed]  # newest unfiled
            if last == day:
                why = "one a day, and one is filed today"
            elif open_finders(p) >= OPEN_CAP:
                why = f"{OPEN_CAP} finder tasks are open, the most at once"
            elif not todo:
                why = "every record is filed already"
            else:
                r = todo[-1]
                where = c.plain(f"{r['file']}:{','.join(str(n) for n in r['lines'][:5] if isinstance(n, int))}")
                b = cli._create(p, c.fit(c.defang(f"fm find missed {c.plain(r['task'])}'s fix in {where}: add a rule, "
                                                    "tool or check"), 120), "FEATURE", "S", "captured", source="discovered",
                                  raw=c.defang(f"No finder flagged this fix's lines (fm find --blindspots; finders that ran: "
                                               f"{', '.join(c.plain(str(x)) for x in r.get('finders') or []) or 'none'})."))
                b.meta.update(finder="blindspot")
                c.save_brief(p, b, touch=False)
                c.log_event(p, "capture", task=b.id, data={"source": "discovered", "finder": "blindspot"})
                c.write_atomic(os.path.join(p.dir, SPOT_DAY), json.dumps(
                    {"day": day, "filed": (filed + [f"{r['task']}|{r['file']}"])[-SPOT_KEEP:]}))
                c.regen_views(p)
                made.append(b.id)
    text = "\n".join(c.plain(f"{r['task']}  {r['file']}:{','.join(str(n) for n in r['lines'][:20])}") for r in rows)  # not a list: line numbers beside one file
    cli.out(args, {"blindspots": rows} | ({"captured": made} if getattr(args, "capture", None) else {}),
              c.plain_lines((text or "No blind spots recorded.") + (f"\nFiled {made[0]}." if made else f"\nFiled 0: {why}." if why else "")))
    return 0


# T-2239, the tool rung: each stack's standard analyzer. The argv is Foreman's, fixed here; a repo's manifest files only
# select an entry (by existing as a regular file, never a symlink). A tool that executes repo code (go vet builds) is why
# the rung runs only where the user said yes (fm prove --yes-run) or in an exam sandbox.
GO_LINE = r"^(?P<file>[^:\s]+\.go):(?P<line>\d+):(?:\d+:)? (?P<msg>.+)$"
TOOLS = (
    {"name": "ruff", "argv": ("ruff", "check", "--output-format", "json", "--no-cache", "--isolated"),
     "selects": ("pyproject.toml", "setup.py", "requirements.txt"), "parse": "ruff", "stream": 0, "kind": "bug"},
    {"name": "go-vet", "argv": ("go", "vet", "./..."), "selects": ("go.mod",), "parse": GO_LINE, "stream": 1, "kind": "bug"},
    {"name": "gosec", "argv": ("gosec", "-fmt", "sarif", "-quiet", "./..."), "selects": ("go.mod",), "parse": "sarif",
     "stream": 0, "kind": "vuln"},
)
TOOL_T, TOOL_MAX = 120, 8 << 20  # a run's seconds, and the bytes each stream may hold (the run is killed past it)


def _rel(path, root):
    """path as a relative name inside root, or None: tool output is data (an absolute path elsewhere, .. or a URI is dropped)."""
    if not isinstance(path, str) or "\x00" in path:
        return None
    path = path[7:] if path.startswith("file://") else path
    if os.path.isabs(path):
        path = os.path.relpath(os.path.normpath(path), root)
    path = os.path.normpath(path)
    return None if path in (".", "") or path == ".." or path.startswith("../") or os.path.isabs(path) else path


def _int(x):
    return x if isinstance(x, int) and not isinstance(x, bool) and x > 0 else 1


def _parse_ruff(text, root):
    return [(r, _int((x.get("location") or {}).get("row")), f"{x.get('code')}: {x.get('message')}")
            for x in json.loads(text) if isinstance(x, dict) and (r := _rel(x.get("filename"), root))]


def _parse_sarif(text, root):
    out = []
    for run in json.loads(text).get("runs") or []:
        for x in run.get("results") or []:
            for loc in (x.get("locations") or [])[:1]:
                phys = loc.get("physicalLocation") or {}
                r = _rel((phys.get("artifactLocation") or {}).get("uri"), root)
                if r:
                    out.append((r, _int((phys.get("region") or {}).get("startLine")),
                                f"{x.get('ruleId')}: {(x.get('message') or {}).get('text')}"))
    return out


def _parse_lines(text, pattern, root):
    rx, out = re.compile(pattern, re.M), []
    for m in rx.finditer(text):
        if r := _rel(m["file"], root):
            out.append((r, _int(int(m["line"])), m["msg"]))
    return out


def _tool_env(root):
    """Sampo's own minimal environment: no credentials, no FOREMAN_/CLAUDE vars. PATH keeps only absolute entries
    outside the tree (a relative one, or one inside it, would run the tree's own file as the tool)."""
    path = os.pathsep.join(x for x in os.environ.get("PATH", "").split(os.pathsep)
                           if os.path.isabs(x) and os.path.commonpath([os.path.realpath(x), root]) != root)
    return {"PATH": path, "HOME": os.environ.get("HOME", "/nonexistent"), "LANG": "C.UTF-8", "CGO_ENABLED": "0",
            "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly", "GOPROXY": "off"}


def _run_tool(argv, root, env):
    """(stdout, stderr, returncode, why): each stream's first TOOL_MAX bytes; why names a timeout or an overflow (the run
    is killed). shell=False; output goes to temp files polled for size (no rlimit: it would reach the tool's own compiler
    and caches); the whole process group is killed however the run ends, so no grandchild outlives it."""
    import signal
    import tempfile
    import time
    why = None
    with tempfile.TemporaryFile() as o, tempfile.TemporaryFile() as e:
        proc = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=o, stderr=e,
                                start_new_session=True)
        try:
            end = time.monotonic() + TOOL_T
            while proc.poll() is None and not why:
                if time.monotonic() > end:
                    why = f"timed out after {TOOL_T}s"
                elif max(os.fstat(o.fileno()).st_size, os.fstat(e.fileno()).st_size) > TOOL_MAX:
                    why = f"output over {TOOL_MAX} bytes"
                else:
                    time.sleep(0.05)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
        texts = []
        for f in (o, e):
            f.seek(0)
            texts.append(f.read(TOOL_MAX).decode("utf-8", "replace"))
        return texts[0], texts[1], proc.returncode, why


# T-2240, the tree-sitter rung: .scm queries as data, only Foreman's own (ladder_rules next to this file; a repo's files
# never add one), run through an optional tree_sitter binding plus tree_sitter_language_pack. Parsing executes nothing.
RULES = os.path.join(os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "ladder_rules")
TS_LANG = {"python": (".py",), "javascript": (".js", ".mjs", ".cjs", ".jsx")}
TS_MAX, TS_T = 512 << 10, 30  # a file's bytes (over it is skipped), and the rung's seconds in all


def _ts_rules(lang):
    """[{"name", "kind", "says", "query"}] from RULES/lang/*.scm: a header of `; kind:` and `; message:` lines, then the query."""
    out, d = [], os.path.join(RULES, lang)
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                text = f.read(1 << 16)
        except OSError:
            continue
        head = dict(m.groups() for m in re.finditer(r"^; (kind|message): (.+)$", text, re.M))
        if n.endswith(".scm") and head.get("message"):
            out.append({"name": n[:-4], "kind": head.get("kind", "bug"), "says": head["message"], "query": text})
    return out


def _ts_rung(root, p=None):
    """(findings, notes): ts:RULE leads. No binding (or pack) is one note and one capability-miss, filed only when the tree
    holds a file of a rule language; a file over TS_MAX, or any link, is not parsed (_files leaves those out)."""
    import time
    files = None
    todo = [(lang, exts, rules) for lang, exts in TS_LANG.items() if (rules := _ts_rules(lang))]
    found, notes, end = [], [], time.monotonic() + TS_T
    for lang, exts, rules in todo:
        if files is None:
            files = _files(root)[0]  # ponytail: a second read of the tree (scan reads it again); share it if that costs
        mine = [(r, t) for r, t in files if r.lower().endswith(exts) and len(t.encode("utf-8", "replace")) <= TS_MAX]
        if not mine:
            continue
        try:
            import tree_sitter
            import tree_sitter_language_pack as pack
        except ImportError:
            notes.append("ts: tree-sitter rung skipped: the tree_sitter binding or tree_sitter_language_pack is not installed")
            if p:
                from sampo import eco
                eco.capture_once(p, "Missing tool tree-sitter: find it (fm plugins find tree-sitter) or build it (script + test)\n"
                                   "capability-miss: tree-sitter", "FEATURE", "self", key="capability-miss: tree-sitter")
            return found, notes
        try:
            language = pack.get_language(lang)
            parser = tree_sitter.Parser(language)
            queries = [(r, language.query(r["query"])) for r in rules]
            for rel, text in mine:
                if time.monotonic() > end:
                    notes.append(f"ts: stopped after {TS_T}s")
                    return found, notes
                data = text.encode("utf-8", "replace")
                root_node = parser.parse(data).root_node
                for r, q in queries:
                    caps = q.captures(root_node)
                    nodes = caps.get("m", []) if isinstance(caps, dict) else [n for n, name in caps if name == "m"]
                    found += [_found("ts:" + r["name"], r["kind"], rel, _int(n.start_point[0] + 1), "", r["says"]) for n in nodes[:50]]
        except Exception as e:  # noqa: BLE001 — a binding, grammar or query that fails is a note, never a crash
            notes.append(f"ts: {lang} rules failed ({type(e).__name__}: {c.fit(c.plain(str(e)), 100)})")
    return found, notes


def ladder(root, p=None, sandbox=False):
    """(findings, notes): the tool rung, then the tree-sitter rung, over root. Tools run nothing unless fmprove.trusted(root)
    or sandbox (a caller's argument: an exam's throwaway render; no file in a tree and no environment variable can set it);
    the tree-sitter rung only parses, so it needs no trust. Findings are tool:NAME and ts:RULE leads; a selected tool not
    on PATH is captured once under the capability-miss key (project p, if any)."""
    from sampo import prove
    root = os.path.realpath(root)  # trust is judged on the resolved path, and the tools run there
    ts, tnotes = _ts_rung(root, p)
    if not (sandbox or prove.trusted(root)):
        return ts, ["tool rung skipped: not a trusted tree (fm prove --yes-run PATH says yes to one)"] + tnotes
    env, found, notes = _tool_env(root), [], tnotes
    for t in TOOLS:
        if not any(stat.S_ISREG(st.st_mode) for m in t["selects"] if (st := _lstat(os.path.join(root, m)))):
            continue
        name = t["argv"][0]
        exe = shutil.which(name, path=env["PATH"])
        if exe and os.path.commonpath([os.path.realpath(exe), root]) == root:  # a link into the tree is the tree's file
            exe = None
        if not exe:
            notes.append(f"tool:{t['name']}: {name} is not on PATH")
            if p:
                from sampo import eco
                eco.capture_once(p, f"Missing tool {name}: find it (fm plugins find {name}) or build it (script + test)\n"
                                   f"capability-miss: {name}", "FEATURE", "self", key=f"capability-miss: {name}")
            continue
        argv = (exe,) + t["argv"][1:]
        if name == "go" and (st := _lstat(os.path.join(root, "vendor", "modules.txt"))) and stat.S_ISREG(st.st_mode):
            argv = argv[:2] + ("-mod=vendor",) + argv[2:]
        out, err, rc, why = _run_tool(argv, root, env)
        first = c.fit(c.plain(next((x for x in err.splitlines() if x.strip()), "")), 120)
        rows = []
        if why:
            notes.append(f"tool:{t['name']}: {why}")
            continue
        try:
            text = (out, err)[t["stream"]]
            rows = (_parse_ruff(text, root) if t["parse"] == "ruff" else _parse_sarif(text, root) if t["parse"] == "sarif"
                    else _parse_lines(text, t["parse"], root))
        except (ValueError, AttributeError, TypeError, RecursionError) as ex:
            notes.append(f"tool:{t['name']}: output not understood ({type(ex).__name__}); exit {rc}; stderr: {first}")
            continue
        if rc < 0 or (rc and not rows):
            notes.append(f"tool:{t['name']}: exit {rc} with {len(rows)} finding(s); stderr: {first}")
        found += [_found("tool:" + t["name"], t["kind"], rel, line, "", c.fit(c.plain(says), 200))
                  for rel, line, says in rows[:500]]
    return found + ts, notes


def _lstat(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def cmd_find(args):
    from sampo import cli
    root, cap = os.path.abspath(args.path), getattr(args, "capture", None)
    ranked = bool(getattr(args, "rank", False) or cap)
    p = c.find_project(os.getcwd()) if ranked or getattr(args, "blindspots", False) else None
    if getattr(args, "blindspots", False):
        if not p:
            print("fm find: --blindspots: run it inside a Sampo project", file=sys.stderr)
            return 2
        return cmd_blindspots(p, args)
    bad = (f"not a directory: {args.path}" if not os.path.isdir(root)
           else "--capture N: N is how many tasks to file, at least 1" if cap is not None and cap < 1
           else "--capture files tasks: run it inside a Sampo project" if cap and not p else None)
    if not bad and getattr(args, "hotspots", False):  # T-2238: where fixes landed before; not findings, so exit 0
        if ranked:
            print("fm find: --hotspots can't combine with --rank or --capture", file=sys.stderr)
            return 2
        got = hotspots(root, 20)
        if got is None:  # a timeout or failure is not a clean result
            print(f"fm find: git failed or timed out under {root} (not a checkout?): no hotspots to list", file=sys.stderr)
            return 2
        rows = [r | {"file": c.defang(c.plain(r["file"]))} for r in got]
        cli.out(args, {"root": root, "hotspots": rows}, "\n".join(f"{r['fixes']:4}  {r['file']}" for r in rows) or f"No fix commits touch tracked files under {root}.")
        return 0
    if bad:  # exit 1 means findings here, so a usage slip must read as an error (2), not as a hit
        print(f"fm find: {bad}", file=sys.stderr)
        return 2
    tools, notes = ladder(root, p) if getattr(args, "ladder", False) else ([], [])
    for note in notes:
        print(f"fm find: {c.plain(note)}", file=sys.stderr)
    try:
        prec = (precision(p) if p else {}) if ranked else None
        n, found = scan(root, prec, proofs(p) if ranked and p else None, p and os.path.realpath(p.root), tools)  # keys use realpaths, as fm prove's do
    except Exception as e:  # noqa: BLE001 — exit 1 means findings; a scan that dies must read as an error (2)
        print(f"fm find: the scan failed under {root}: {type(e).__name__}: {c.plain(str(e))[:300]}", file=sys.stderr)
        return 2
    made = capture(p, found, cap, root) if cap else None
    if cap:
        save_flags(p, found, root)  # T-2241
    if ranked and p:  # T-2488: the rank's keep-or-hold call per lead, for the model's shadow pass (top 20 a scan)
        from sampo import models
        for f in found[:20]:
            models.shadow(p, "lead-triage", f"{f['kind']} {f['file']}:{f['line']} {f['symbol'] or '-'}: {f['says']}",
                            "noise" if f.get("held") else "real")
    rows = [(f"{f['score']:.2f}  " if "score" in f else "") + f"{f['file']}:{f['line']}  {f['kind']}  "
            f"{f['symbol'] or '-'}: {f['says']}" + (f"  ({f['held']})" if f.get("held") else "") for f in found]
    live = [f for f in found if not f.get("held")]
    held = f" {len(found) - len(live)} more held back (a muted finder or a score under the bar), listed last." \
        if len(live) < len(found) else ""
    cli.out(args, {"root": root, "files": n, "findings": found} | ({"captured": made} if cap else {}), c.plain_lines(
        f"{len(live)} candidate(s) in {n} files under {root}; each is unproven until a failing test shows it.{held}\n"
        + "\n".join(rows) if found else f"Nothing found in {n} files under {root}." if n else
        f"No files to read under {root} (nothing tracked, or all of it skipped).")
        + (filed_line(p, made) if cap else ""))
    return 1 if live else 0
