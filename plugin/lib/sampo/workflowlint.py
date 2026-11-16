"""T-2564: a local actionlint for the repo's GitHub workflow files, with no install and no network. A workflow typo used to
reach CI only after the push; `python3 -I plugin/lib/sampo/workflowlint.py [FILE…]` (default .github/workflows/*.yml and
*.yaml under the current folder) prints `file:line: finding` and exits 1 on any. No YAML library is installed, so a small
reader (`load`) covers what workflows use: block and flow maps and sequences, plain and multi-line quoted scalars, | and >
block scalars, comments, document markers, anchors, aliases and `<<` merge keys (a tag is ignored, with a note). It does not
read tabs in indentation or text GitHub's own parser rejects (a `: ` inside a plain value, a plain value starting with @, % or
a backtick, a second document): such a file is reported at its line, never passed. The lint names: a parse error; a missing
on, jobs, runs-on or steps; a step without exactly one of run and uses; a uses with an empty @ref; an unclosed ${{; a needs
naming no job, and a needs cycle; a matrix, needs or steps reference to nothing; and a run block bash -n rejects (expressions
replaced by a placeholder that keeps the line count, the line the line inside the file; a block that sets extglob, or a run
for a runner that may be Windows, is not given to bash). A `}}` with no `${{` is literal text to GitHub, so it is not named.
Work is bounded by the file size (1 MB), the nesting depth and the bash budget. Stdlib only: the file runs on its own."""
# map-links: plugin/tests/test_workflow_lint.py
import glob
import os
import re
import shutil
import subprocess
import sys
import time

MAX_BYTES = 1_000_000  # a workflow file is a few KB; a bigger one is refused, not read
MAX_DEPTH = 60  # block collections nested inside each other
MAX_FLOW = 100_000  # characters in one [ ] or { } collection
MAX_NEST = 20  # flow collections nested inside each other
MAX_MERGED = 200_000  # entries all `<<` merge keys of a file may copy
MAX_BASH = 200 # run blocks given to bash -n per file; the rest are named as not checked
MAX_WORK = 200_000  # steps, values and needs read across all jobs of a file (aliases share a job, not the work of reading it again)
MAX_FINDINGS = 200  # findings listed per file; the rest are counted in one last line
MAX_UNCLOSED = 20 # unclosed `${{` named per value (each costs a line count over the value)
BASH_TIMEOUT = 10 # seconds per bash -n
BASH_TOTAL = 40  # seconds of bash -n across one file; the rest are named as not checked


class YamlError(Exception):
    def __init__(self, line, msg):
        super().__init__(msg)
        self.line = line


class Str(str):
    line = 0


class Map(dict):
    """A mapping that remembers the line of each key."""
    line = 0

    def __init__(self):
        super().__init__()
        self.lines = {}

    def line_of(self, key):
        return self.lines.get(key, self.line)


class Seq(list):
    line = 0

    def __init__(self):
        super().__init__()
        self.lines = []


_BLOCK = re.compile(r"^([|>])([+-]?)\d?\s*(?:#.*)?$")
_COLON = re.compile(r":(?=\s|$)")  # a mapping colon: followed by a blank or the end of the line
_COMMENT = re.compile(r"(^|\s)#")
_MARK = re.compile(r"(---|\.\.\.)(?:\s+#.*|\s*)$")  # a document marker line
_ESC = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
_ESC_HEX = {"x": 2, "u": 4, "U": 8}
_ESC_OK = frozenset("0abt\tnvfre \"/\\N_LPxuU")  # the escape letters YAML 1.2 knows in a double-quoted scalar


def _str(s, line):
    out = Str(s)
    out.line = line
    return out


def _uncomment(s):
    """A plain scalar up to its ` #` comment (YAML ends a plain scalar there, quotes or not)."""
    m = _COMMENT.search(s)
    return (s[:m.start()] if m else s).strip()


def _quoted(s, line, more=None):
    """(value, rest after the closing quote, lines read past this one) of the quoted scalar that starts s. `more()` gives the next
    raw line (None past the last) for a scalar that goes on over lines: YAML folds a break to a space (a blank line to a newline)
    and a backslash at the end of a double-quoted line joins it to the next."""
    q = s[0]
    k, out, extra, cont = 1, [], 0, False
    while True:
        while k < len(s):
            ch = s[k]
            if q == '"' and ch == "\\":
                if k + 1 == len(s):
                    cont = True
                    k += 1
                    break
                if s[k + 1] not in _ESC_OK:
                    raise YamlError(line + extra, f"'\\{s[k + 1]}' is not a YAML escape in a double-quoted string (write \\\\)")
                width = _ESC_HEX.get(s[k + 1], 0)
                if width and not re.fullmatch(r"[0-9A-Fa-f]{%d}" % width, s[k + 2:k + 2 + width]):
                    raise YamlError(line + extra, f"'\\{s[k + 1]}' needs {width} hex digits in a double-quoted string "
                                                  "(write \\\\ for a backslash)")
                out.append(_ESC.get(s[k + 1], "\\" + s[k + 1]))
                k += 2
                continue
            if ch == q:
                if q == "'" and s[k + 1:k + 2] == "'":
                    out.append("'")
                    k += 2
                    continue
                return "".join(out), s[k + 1:], extra
            out.append(ch)
            k += 1
        nxt = more() if more else None
        if nxt is None:
            raise YamlError(line, "a quoted scalar is never closed")
        extra += 1
        blanks = 0
        while not nxt.strip():
            blanks += 1
            nxt = more()
            if nxt is None:
                raise YamlError(line, "a quoted scalar is never closed")
            extra += 1
        if cont:
            out.append("\n" * blanks)
            cont = False
        else:
            while out and out[-1] in " \t":
                out.pop()
            out.append("\n" * blanks if blanks else " ")
        s, k = nxt.lstrip(" \t"), 0


def _key(s):
    """(key, rest) when the line content s is `key: rest` (a plain or quoted key), else None. One pass over s."""
    if not s:
        return None
    c = s[0]
    if c in "\"'":
        try:
            key, tail, _ = _quoted(s, 0)
        except YamlError:
            return None
        tail = tail.lstrip()
        if not tail.startswith(":") or (len(tail) > 1 and tail[1] not in " \t"):
            return None
        return key, tail[1:].strip()
    if c in " \t#[]{},&*!|>%@`" or (c == "-" and (len(s) == 1 or s[1] in " \t#")):
        return None
    m = _COLON.search(s)
    if not m:
        return None
    key = s[:m.start()].rstrip()
    if not key or _COMMENT.search(key):
        return None
    return key, s[m.end():].strip()


def _flow_feed(s, st, line):
    """Scan one more line s of a flow collection; st = [depth, quote] carries over. (text kept, tail after the closing bracket
    or None while the collection is open); a comment ends the kept text."""
    depth, q = st
    i = 0
    while i < len(s):
        ch = s[i]
        if q:
            if ch == q:
                if q == "'" and s[i + 1:i + 2] == "'":
                    i += 1
                else:
                    q = None
            elif q == '"' and ch == "\\":
                i += 1
        elif ch in "'\"" and (i == 0 or s[i - 1] in " \t[{,:"):
            q = ch
        elif ch in "[{":
            depth += 1
            if depth > MAX_NEST:
                raise YamlError(line, f"flow collections nested over {MAX_NEST} deep are not read")
        elif ch in "]}":
            depth -= 1
            if depth <= 0:
                st[:] = [0, None]
                return s[:i + 1], s[i + 1:]
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            st[:] = [depth, q]
            return s[:i], None
        i += 1
    st[:] = [depth, q]
    return s, None


def _split_flow(body):
    parts, depth, q, cur = [], 0, None, []
    for i, ch in enumerate(body):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"" and (i == 0 or body[i - 1] in " \t[{,:"):
            q = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip() or parts:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _props(s, line):
    """(anchor names, rest) of s without its leading `&name` and `!tag` properties; a tag is dropped (its name goes in tags)."""
    names, tags = [], []
    while s[:1] in ("&", "!") and s:
        m = re.match(r"([&!])(\S*)\s*(.*)$", s, re.S)
        if m.group(1) == "&":
            if not m.group(2):
                raise YamlError(line, "an anchor with no name")
            names.append(m.group(2))
        else:
            tags.append(m.group(2))
        s = m.group(3)
    return names, tags, s


def _alias(s, line, anchors):
    name = s[1:].strip()
    if not name or re.search(r"\s", name):
        raise YamlError(line, "an alias is *name and nothing after it")
    if name not in anchors:
        raise YamlError(line, f"alias *{name} has no &{name} anchor before it")
    return anchors[name]


def _flow(s, line, anchors):
    s = s.strip()
    names, _, s = _props(s, line)
    if not s:
        val = _str("", line)
    elif s[0] == "*":
        val = _alias(s, line, anchors)
    elif s[0] == "[":
        val = Seq()
        val.line = line
        for part in _split_flow(s[1:-1]):
            val.append(_flow(part, line, anchors))
            val.lines.append(line)
    elif s[0] == "{":
        val = Map()
        val.line = line
        for part in _split_flow(s[1:-1]):
            k, sep, v = part.partition(":")
            if not sep:
                raise YamlError(line, f"flow map entry '{part}' has no ':'")
            k = k.strip().strip("'\"")
            if k in val:
                raise YamlError(line, f"duplicate key '{k}'")
            val[k] = _flow(v.strip(), line, anchors) if v.strip() else _str("", line)
            val.lines[k] = line
    elif s[0] in "'\"":
        v, rest, _ = _quoted(s, line)
        if rest.strip():
            raise YamlError(line, "text after a closing quote")
        val = _str(v, line)
        val.folded = True
    else:
        val = _str(s, line)
    for n in names:
        anchors[n] = val
    return val


class _Reader:
    def __init__(self, text):
        self.raw = text.replace("\r\n", "\n").split("\n")
        self.over = {}  # a `- ` item's rest, read as a line of its own at its own indent
        self.anchors = {}  # &name -> the value it marks, for *name and <<: *name
        self.notes = []  # what was read in a lossy way (a tag), never a finding
        self.depth = 0
        self.merged = 0  # entries copied by merge keys so far

    def markers(self):
        """Blank the leading `---`, cut the text at `...`, refuse a second document: one workflow per file."""
        seen = False
        for n, ln in enumerate(self.raw):
            m = _MARK.match(ln)
            if m:
                if m.group(1) == "...":
                    rest = self.raw[n + 1:]
                    if any(x.strip() and not x.lstrip().startswith("#") for x in rest):
                        raise YamlError(n + 2, "text after the document end '...': a workflow file holds one document")
                    del self.raw[n:]
                    return
                if seen:
                    raise YamlError(n + 1, "a second YAML document: a workflow file holds one")
                self.raw[n] = ""
                seen = True
            elif ln.strip() and not ln.lstrip().startswith("#"):
                seen = True

    def get(self, i):
        if i in self.over:
            return self.over[i]
        s = self.raw[i]
        body = s.lstrip(" ")
        return len(s) - len(body), body.rstrip()

    def skip(self, i):
        while i < len(self.raw):
            s = self.raw[i]
            body = s.strip()
            if body and not body.startswith("#"):
                lead = s[:len(s) - len(s.lstrip())]
                if "\t" in lead:
                    raise YamlError(i + 1, "a tab in the indentation: YAML indents with spaces")
                return i
            i += 1
        return i

    def block(self, i, parent):
        i = self.skip(i)
        if i >= len(self.raw):
            return None, i
        ind, content = self.get(i)
        if ind <= parent:
            return None, i
        if content == "-" or content.startswith("- "):
            return self.seq(i, ind)
        return self.map(i, ind)

    def _nest(self, fn, i, indent):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise YamlError(i + 1, f"collections nested over {MAX_DEPTH} deep are not read")
        out = fn(i, indent)
        self.depth -= 1
        return out

    def map(self, i, indent):
        return self._nest(self._map, i, indent)

    def seq(self, i, indent):
        return self._nest(self._seq, i, indent)

    def _map(self, i, indent):
        m = Map()
        m.line = i + 1
        merges = []
        while True:
            i = self.skip(i)
            if i >= len(self.raw):
                break
            ind, content = self.get(i)
            if ind < indent:
                break
            if ind > indent:
                raise YamlError(i + 1, "unexpected indentation")
            km = _key(content)
            if not km or content == "-" or content.startswith("- "):
                raise YamlError(i + 1, "expected 'key: value'")
            key, rest = km
            if key in m:
                raise YamlError(i + 1, f"duplicate key '{key}'")
            if key == "if" and rest.lstrip().startswith("!"):  # YAML reads `!name` as a tag: the condition would be empty
                raise YamlError(i + 1, "an if: condition that starts with '!' reads as a YAML tag, not a condition: "
                                       "write it as ${{ !… }} (GitHub needs the braces here)")
            if key == "<<":
                self.notes.append(f"line {i + 1}: the merge key '<<' is read here, but GitHub's support for it is not certain: "
                                  "check it against GitHub")
            val, i2 = self.value(i, indent, rest, in_map=True)
            if key == "<<":  # a merge key: its mapping(s) fill in the keys this one doesn't set itself
                parts = list(val) if isinstance(val, Seq) else [val]
                if not parts or not all(isinstance(p, Map) for p in parts):
                    raise YamlError(i + 1, "a merge key '<<' takes a mapping (or a list of mappings)")
                merges += [(p, i + 1) for p in parts]
            else:
                m[key] = val
                m.lines[key] = i + 1
            i = i2
        for p, line in merges:
            self.merged += len(p)
            if self.merged > MAX_MERGED:
                raise YamlError(line, f"merge keys that copy over {MAX_MERGED} entries are not read")
            for k, v in p.items():
                if k not in m:
                    m[k] = v
                    m.lines[k] = line
        return m, i

    def _seq(self, i, indent):
        s = Seq()
        s.line = i + 1
        while True:
            i = self.skip(i)
            if i >= len(self.raw):
                break
            ind, content = self.get(i)
            if ind < indent or not (content == "-" or content.startswith("- ")):
                if ind > indent:
                    raise YamlError(i + 1, "unexpected indentation")
                break
            if ind > indent:
                raise YamlError(i + 1, "unexpected indentation")
            rest = content[1:]
            body = rest.lstrip(" ")
            s.lines.append(i + 1)
            if not body or body.startswith("#"):
                j = self.skip(i + 1)
                if j < len(self.raw) and self.get(j)[0] > indent:
                    val, i = self.block(j, indent)
                else:
                    val, i = _str("", i + 1), i + 1
            else:
                off = indent + 1 + len(rest) - len(body)
                self.over[i] = (off, body)
                if body == "-" or body.startswith("- "):  # `- - a`: a sequence inside the item
                    val, i = self.seq(i, off)
                elif _key(body):
                    val, i = self.map(i, off)
                else:
                    val, i = self.value(i, indent, body, in_map=False)
            s.append(val)
        return s, i

    def value(self, i, indent, rest, in_map):
        """(value, next line) of what follows a key (or a `- `) on line i, whose parent sits at indent: an optional `&anchor`
        and `!tag`, then an alias or the value itself."""
        names, tags, rest = _props(rest.strip(), i + 1)
        for t in tags:
            self.notes.append(f"line {i + 1}: the tag '!{t}' is ignored (the value is read as it is written)")
        if rest.startswith("*"):
            tail = rest.split("#", 1)[0] if " #" in rest else rest
            val, j = _alias(tail, i + 1, self.anchors), i + 1
        else:
            val, j = self._value(i, indent, rest, in_map)
        for n in names:
            self.anchors[n] = val
        return val, j

    def _value(self, i, indent, rest, in_map):
        line = i + 1
        rest = rest.strip()
        if not rest or rest.startswith("#"):
            j = self.skip(i + 1)
            if j < len(self.raw):
                ind2, c2 = self.get(j)
                if ind2 > indent:
                    val, j2 = self.block(j, indent)
                    return val, j2
                if in_map and ind2 == indent and (c2 == "-" or c2.startswith("- ")):
                    return self.seq(j, indent)
            return _str("", line), i + 1
        bm = _BLOCK.match(rest)
        if bm:
            return self.block_scalar(i, indent, bm.group(1), bm.group(2))
        if rest[0] in "[{":
            st, j, size = [0, None], i + 1, 0
            kept, tail = _flow_feed(rest, st, line)
            parts = [kept]
            size = len(kept)
            while tail is None:
                if j >= len(self.raw):
                    raise YamlError(line, "a [ or { is never closed")
                kept, tail = _flow_feed(self.raw[j].strip(), st, j + 1)
                parts.append(kept)
                size += len(kept)
                if size > MAX_FLOW:
                    raise YamlError(line, f"a [ or {{ collection over {MAX_FLOW} characters is not read")
                j += 1
            if size > MAX_FLOW:
                raise YamlError(line, f"a [ or {{ collection over {MAX_FLOW} characters is not read")
            tail = tail.strip()
            if tail and not tail.startswith("#"):
                raise YamlError(line, "text after a closing bracket")
            return _flow(" ".join(parts), line, self.anchors), j
        if rest[0] in "'\"":
            pos = [i + 1]

            def more():
                if pos[0] >= len(self.raw):
                    return None
                pos[0] += 1
                return self.raw[pos[0] - 1]
            v, tail, extra = _quoted(rest, line, more)
            tail = tail.strip()
            if tail and not tail.startswith("#"):
                raise YamlError(line + extra, "text after a closing quote")
            val = _str(v, line)
            val.folded = True  # its newlines are not file lines
            return val, i + 1 + extra
        if rest[0] in "|>":
            raise YamlError(line, "a block scalar header is | or > with an optional indent digit and +/-")
        if rest[0] in "@%`":
            raise YamlError(line, f"a plain value can't start with '{rest[0]}' (quote it)")
        first = _uncomment(rest)
        if ": " in first or first.endswith(":") or ":\t" in first:
            raise YamlError(line, "a ': ' inside a plain value is a mapping key there (quote the value if the colon is text)")
        parts, j = [first], i + 1
        while True:  # a plain scalar goes on over deeper-indented lines
            k = self.skip(j)
            if k >= len(self.raw) or self.get(k)[0] <= indent:
                break
            more = _uncomment(self.get(k)[1])  # a ': ' in a trailing comment is no mapping key
            if ": " in more or ":\t" in more or more.endswith(":"):
                raise YamlError(k + 1, "a mapping key inside a plain value: check the indentation")
            parts.append(more)
            j = k + 1
        return _str(" ".join(parts), line), j

    def block_scalar(self, i, indent, style, chomp):
        j = i + 1
        body = []
        while j < len(self.raw):
            s = self.raw[j]
            if s.strip() and len(s) - len(s.lstrip(" ")) <= indent:
                break
            body.append(s)
            j += 1
        while body and not body[-1].strip():  # trailing blank lines belong to the chomping, not the text
            body.pop()
        first = next((s for s in body if s.strip()), "")
        width = len(first) - len(first.lstrip(" "))
        lines = [s[width:] if s.strip() else "" for s in body]
        if style == ">":  # folded: two plain lines join with a space, a blank line is a break, and a more-indented line keeps its breaks
            out, prev, blank = [], None, 0
            for ln in lines:
                if not ln:
                    blank += 1
                    continue
                if prev is None:
                    out.append("\n" * blank)
                else:
                    spaced = ln[0] in " \t" or prev[0] in " \t"
                    out.append("\n" * (blank + 1) if blank and spaced else "\n" * blank if blank else "\n" if spaced else " ")
                out.append(ln)
                prev, blank = ln, 0
            text = "".join(out)
        else:
            text = "\n".join(lines)
        text += "" if chomp == "-" or not lines else "\n"
        val = _str(text, i + 2)
        val.folded = style == ">"
        return val, j


def load(text, notes=None):
    """The workflow text as Map, Seq and Str values that know their lines; YamlError names the line it can't read. notes (a list)
    collects what was read in a lossy way."""
    r = _Reader(text[1:] if text.startswith("﻿") else text)  # a byte order mark is not part of the first key
    r.markers()
    i = r.skip(0)
    if i >= len(r.raw):
        return Map()
    val, j = r.block(i, -1)
    j = r.skip(j)
    if j < len(r.raw):
        raise YamlError(j + 1, "unexpected indentation")
    if notes is not None:
        notes.extend(r.notes)
    return val


# ---- the lint -------------------------------------------------------------------------------------------------------

def _exprs(text):
    """(expressions [(offset, body)], unclosed `${{` offsets) of text; a `}}` inside a '…' literal is part of it. A `}}` with no
    `${{` before it is literal text to GitHub, so it is not reported. One pass: a `${{` met before its `}}` was never closed."""
    out, unclosed, i = [], [], 0
    while True:
        a = text.find("${{", i)
        if a < 0:
            break
        j, q, again = a + 3, False, False
        while j < len(text):
            if text[j] == "'":
                q = not q
            elif text.startswith("${{", j):  # the first one was never closed: the next one's }} closes this one
                unclosed.append(a)
                i, again = j, True
                break
            elif not q and text.startswith("}}", j):
                break
            j += 1
        else:
            unclosed.append(a)
            break
        if again:
            continue
        out.append((a, text[a + 3:j]))
        i = j + 2
    return out, unclosed


def _leaves(node, key=None, seen=None):
    """(key, Str) of every string value under node; a collection an alias repeats is walked once."""
    seen = set() if seen is None else seen
    if isinstance(node, (Map, Seq)):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, Map):
            for k, v in node.items():
                yield from _leaves(v, k, seen)
        else:
            for v in node:
                yield from _leaves(v, key, seen)
    elif isinstance(node, Str):
        yield key, node


def _at(s, offset):
    """The file line of an offset in a scalar (block scalars keep their lines; folded and plain ones sit on one)."""
    return s.line + (0 if getattr(s, "folded", False) else s.count("\n", 0, offset))


def _names(v):
    """The job names a needs value lists."""
    if isinstance(v, Seq):
        return [str(x) for x in v]
    return [str(v)] if isinstance(v, str) and v else []


def _matrix_keys(job):
    """The keys job's strategy.matrix defines, or None when it is dynamic (an expression) or absent."""
    st = job.get("strategy")
    if isinstance(st, Str) and st:  # strategy: ${{ fromJSON(…) }}: the keys are not known here
        return "dynamic"
    if not isinstance(st, Map) or "matrix" not in st:
        return None
    mx = st["matrix"]
    if not isinstance(mx, Map):
        return "dynamic"
    keys = {k for k in mx if k not in ("include", "exclude")}
    inc = mx.get("include")
    if isinstance(inc, Seq):
        for e in inc:
            if isinstance(e, Map):
                keys |= set(e)
            else:
                return "dynamic"
    elif inc is not None:
        return "dynamic"
    return keys


def _matrix_size(job):
    """How many entries job's matrix holds (its keys, their values and its include list): the work reading it costs."""
    st = job.get("strategy")
    mx = st.get("matrix") if isinstance(st, Map) else None
    if not isinstance(mx, Map):
        return 0
    return len(mx) + sum(len(v) for v in mx.values() if isinstance(v, Seq))


def _matrix_values(job, key):
    """The strings the job's static matrix gives `key` (its list and its include entries), or None when any is not known."""
    st = job.get("strategy")
    mx = st.get("matrix") if isinstance(st, Map) else None
    if not isinstance(mx, Map) or key not in mx.keys() | {k for e in (mx.get("include") or []) if isinstance(e, Map) for k in e}:
        return None
    vals = []
    inc = mx.get("include")
    for v in ([mx[key]] if key in mx else []) + [e[key] for e in (inc if isinstance(inc, Seq) else []) if isinstance(e, Map) and key in e]:
        for x in (v if isinstance(v, Seq) else [v]):
            if not isinstance(x, Str) or "${{" in x:
                return None
            vals.append(str(x))
    return vals


def _labels(job):
    """The runner labels job's runs-on may name, or None when an expression leaves them open (a static matrix key is resolved)."""
    out = []
    for _, s in _leaves(job.get("runs-on")):
        if "${{" not in s:
            out.append(str(s))
            continue
        m = re.fullmatch(r"\s*\$\{\{\s*matrix\.([\w-]+)\s*\}\}\s*", s)
        vals = _matrix_values(job, m.group(1)) if m else None
        if vals is None:
            return None
        out += vals
    return out


def _runner_bash(job):
    """True when a run with no shell set is bash on job's runner. Its default shell is pwsh on Windows, so False for a windows
    label, an expression left open, or a self-hosted runner naming no other OS."""
    labels = _labels(job)
    if labels is None:
        return False
    low = [x.lower() for x in labels]
    if any("windows" in x or x.startswith("win") for x in low):
        return False
    return not ("self-hosted" in low and not any(("ubuntu" in x or "linux" in x or "mac" in x) for x in low))


def _shell_ok(step, job, doc, runner=None):
    """False when the step's run is not for bash: another shell is set (pwsh, python, cmd…), or none is set and the runner may be
    Windows. bash -n can't judge such a run. `runner` is a one-item list that keeps _runner_bash's answer for the job's steps."""
    for node in (step, ((job.get("defaults") or {}).get("run") if isinstance(job.get("defaults"), Map) else None),
                 ((doc.get("defaults") or {}).get("run") if isinstance(doc.get("defaults"), Map) else None)):
        sh = node.get("shell") if isinstance(node, Map) else None
        if sh:
            return str(sh).split()[0] in ("bash", "sh")
    if runner is None:
        return _runner_bash(job)
    if not runner:
        runner.append(_runner_bash(job))
    return runner[0]


def _bash(script):
    """(line inside the script or None, message) when bash -n rejects it; None when it parses."""
    p = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, timeout=BASH_TIMEOUT)
    if p.returncode == 0:
        return None
    err = (p.stderr.strip().splitlines() or ["rejected"])[0]
    m = re.search(r"line (\d+):\s*(.*)", err)
    return (int(m.group(1)) if m else None, (m.group(2) if m else err)[:160])


def _cycles(graph, jobs):
    """[(line, finding)] for each needs cycle: an iterative depth-first walk over every need, so a job with two needs and a
    deep chain are both followed. Each back edge found is one cycle, named from where it closes."""
    found, state, pos = [], {}, {}  # state: 1 on the current path, 2 done
    for start in graph:
        if start in state:
            continue
        stack = [(start, iter(graph[start]))]
        state[start], pos[start] = 1, 0
        path = [start]
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if nxt not in graph:
                    continue
                if state.get(nxt) == 1:
                    if len(found) >= MAX_FINDINGS:
                        return found
                    cyc = path[pos[nxt]:] + [nxt]
                    found.append((jobs.line_of(nxt), "needs cycle: " + " -> ".join(cyc)))
                elif nxt not in state:
                    state[nxt], pos[nxt] = 1, len(path)
                    path.append(nxt)
                    stack.append((nxt, iter(graph[nxt])))
                    break
            else:
                state[node] = 2
                path.pop()
                stack.pop()
    return found


def lint_text(text, notes=None):
    """[(line, finding)] for one workflow's text, in line order; notes (a list) collects what was not checked."""
    notes = notes if notes is not None else []
    try:
        return _lint(text, notes)
    except RecursionError:
        return [(1, "YAML not read: nested too deeply")]


def _lint(text, notes):
    found = []
    try:
        doc = load(text, notes)
    except YamlError as e:
        return [(e.line, f"YAML not read: {e}")]
    if not isinstance(doc, Map):
        return [(1, "a workflow file is a mapping with on: and jobs:")]
    if "on" not in doc and "true" not in doc:
        found.append((1, "no 'on:' (what triggers the workflow)"))
    jobs = doc.get("jobs")
    if not isinstance(jobs, Map) or not jobs:
        found.append((doc.line_of("jobs"), "no 'jobs:' (a mapping with at least one job)"))
        jobs = Map()
    graph, done = {}, {}  # done: id(job) -> its needs, for a job an alias repeats under another name
    bash_runs, bash_capped, t0, work = 0, False, time.monotonic(), 0
    for jname, job in jobs.items():
        jl = jobs.line_of(jname)
        if not isinstance(job, Map):
            found.append((jl, f"job '{jname}' is not a mapping"))
            continue
        if id(job) in done:  # an alias of a job already linted: same findings, so only its place in the needs graph is new
            graph[jname] = done[id(job)]
            work += 1 + len(done[id(job)])
            if work > MAX_WORK:
                break
            continue
        needs = _names(job.get("needs"))
        graph[jname] = done[id(job)] = needs
        work += 1 + len(needs) + _matrix_size(job)
        if work > MAX_WORK:
            break
        for n in needs:
            if n not in jobs:
                found.append((job.line_of("needs"), f"needs names no job: '{n}'"))
        reusable = "uses" in job
        if not reusable and "runs-on" not in job:
            found.append((jl, f"job '{jname}' has no runs-on"))
        steps = job.get("steps")
        ids = set()
        if not reusable:
            if not isinstance(steps, Seq) or not steps:
                found.append((job.line_of("steps") if "steps" in job else jl, f"job '{jname}' has no steps"))
                steps = Seq()
        else:
            steps = Seq()
        runner = []
        for idx, step in enumerate(steps):
            work += 1
            if work > MAX_WORK:
                break
            sl = steps.lines[idx]
            if not isinstance(step, Map):
                found.append((sl, "a step is a mapping (run: or uses:)"))
                continue
            if isinstance(step.get("id"), str):
                ids.add(str(step["id"]))
            has = [k for k in ("run", "uses") if k in step]
            if len(has) != 1:
                found.append((sl, f"a step needs exactly one of run or uses (has {'both' if has else 'neither'})"))
            u = step.get("uses")
            if isinstance(u, str) and u and "${{" not in u and not u.startswith(("./", "docker://")):
                path_, at_, ref = u.rpartition("@")
                if not at_:
                    found.append((step.line_of("uses"), f"uses '{u}' has no @ref (pin a tag or sha)"))
                elif not path_.strip("/") or not ref:
                    found.append((step.line_of("uses"), f"uses '{u}' has an empty " + ("ref" if path_ else "action path")))
            run = step.get("run")
            if isinstance(run, Str) and not bash_capped and _shell_ok(step, job, doc, runner):
                ex, unclosed = _exprs(run)
                if unclosed:
                    continue  # named below as an unbalanced expression; bash would only echo it
                if bash_runs >= MAX_BASH or time.monotonic() - t0 > BASH_TOTAL:
                    bash_capped = True  # one note for the file, not one per run block
                    notes.append(f"run blocks past the first {bash_runs} are not given to bash -n")
                    continue
                bash_runs += 1
                script = run
                for a, body in reversed(ex):  # EXPR keeps the line count of the text it replaces
                    script = script[:a] + "EXPR" + "\n" * body.count("\n") + script[a + 5 + len(body):]
                if shutil.which("bash") is None:
                    bash_capped = True
                    notes.append("bash not on PATH: run blocks not checked")
                    continue
                try:
                    bad = _bash(script)
                except subprocess.TimeoutExpired:
                    found.append((run.line, f"bash -n took over {BASH_TIMEOUT} s on this run block: not checked"))
                    continue
                if bad and "shopt -s extglob" in script:  # bash -n reads the extended patterns before shopt runs
                    notes.append(f"{jname}: a run block with 'shopt -s extglob' is not checked (bash -n can't read it)")
                    bad = None
                if bad:
                    n, msg = bad
                    nlines = len(script.rstrip("\n").split("\n"))
                    inner = min(n or 1, nlines)  # an unexpected end of file is reported past the last line
                    found.append((run.line + (0 if getattr(run, "folded", False) else inner - 1), f"bash -n: {msg}"))
        if work > MAX_WORK:
            break
        mkeys = _matrix_keys(job)
        for key, s in _leaves(job):
            work += 1
            if work > MAX_WORK:
                break
            ex, unclosed = _exprs(s)
            if key == "if" and not ex and not unclosed:
                ex = [(0, str(s))]  # a bare condition is an expression without ${{ }}
            for a, body in ex:
                body = re.sub(r"'[^']*'", "''", body)
                for kind, name in re.findall(r"(?<![\w.])(matrix|needs|steps)\.([A-Za-z_][\w-]*)", body):
                    line = _at(s, a)
                    if kind == "matrix" and mkeys != "dynamic" and (mkeys is None or name not in mkeys):
                        found.append((line, f"matrix.{name}: " + ("the job has no strategy.matrix" if mkeys is None
                                                                  else f"no matrix key or include entry defines '{name}'")))
                    elif kind == "needs" and name not in needs:
                        found.append((line, f"needs.{name} names no job in this job's needs"))
                    elif kind == "steps" and name not in ids:
                        found.append((line, f"steps.{name} names no step id in this job"))
    if work > MAX_WORK:
        found.append((jobs.line_of(jname), f"over {MAX_WORK} steps, values and needs in this file (jobs that alias one another share "
                                           "the reading, not a long list they all name): the rest is not checked"))
    unclosed_named = 0
    for key, s in _leaves(doc):  # a `${{` never closed, in every value of the file (a stray `}}` is literal text to GitHub)
        if unclosed_named >= MAX_FINDINGS:
            break
        for a in _exprs(s)[1][:MAX_UNCLOSED]:
            unclosed_named += 1
            found.append((_at(s, a), "unbalanced expression: '${{' is never closed with '}}'"))
    found += _cycles(graph, jobs)[:MAX_FINDINGS]
    out = sorted(set(found))
    if len(out) > MAX_FINDINGS:
        out = out[:MAX_FINDINGS] + [(out[MAX_FINDINGS - 1][0], f"{len(out) - MAX_FINDINGS} more findings not listed")]
    return out


def lint_file(path, notes=None):
    """[(line, finding)] for one workflow file; one finding at line 1 when it can't be read."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return [(1, f"too large ({os.path.getsize(path)} bytes, over {MAX_BYTES}): not checked")]
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            local = []
            found = lint_text(f.read(), local)
        if notes is not None:
            notes.extend(f"{path}: {n}" for n in local)
        return found
    except OSError as e:
        return [(1, f"can't read: {e.strerror or e}")]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    files = argv or sorted(glob.glob(".github/workflows/*.yml") + glob.glob(".github/workflows/*.yaml"))
    notes, bad = [], 0
    for f in files:
        for line, msg in lint_file(f, notes):
            print(f"{f}:{line}: {msg}")
            bad += 1
    for n in dict.fromkeys(notes):
        print(f"note: {n}")
    if not bad:
        print(f"workflow lint: {len(files)} file(s) ok" if files else "workflow lint: no workflow files")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
