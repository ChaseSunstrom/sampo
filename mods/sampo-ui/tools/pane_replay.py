#!/usr/bin/env python3
"""foreman-ui pane replay (T-1534): the Sampo pane drawn from a recorded view with no live session, through the mod's
own test kit (claude plugin test) and its fakes, and a check that two runs of one fixture draw the same bytes.

  pane_replay.py render FIXTURE [--cols 80] [--rows 40] [--json]  the pane as text (--json: the drawn tree)
  pane_replay.py twin FIXTURE [--cols 80] [--rows 40] [--matrix]  two renders, each its own process, diffed: exit 1
                                                                  and the diff when they differ. --matrix: a third in
                                                                  another time zone and locale
  pane_replay.py svg FIXTURE --out FILE [--cols 80] [--rows 40]   the text frame as an SVG (a .png through
                                                                  rsvg-convert), for docs and reviews (T-1670)
  pane_replay.py golden [--update]                                the pane drawn from tests/fixtures/pane-busy.ts at
                                                                  80x40 against tests/fixtures/golden.ts, which the
                                                                  kit's replay test checks too: exit 1 and the diff
                                                                  when it drifted; --update rewrites it (T-1670)

A fixture is what `fm ui --json` prints, the pane's whole input: a .json file, or a .ts module like
tests/fixtures/pane-busy.ts whose text after `export default` is that JSON (read as data, never run). Recording one
(`fm ui --json > view.json`) copies the project's real titles, so keep recordings out of the repo: tests/fixtures/
holds synthetic ones only. Each run copies the mod into a temporary folder with tests/replay.test.tsx as its only test
and its own tests/fixtures/replay.ts (the view, the size, print: true); that test fakes fm, the clock (frozen:
mock.clock), the store and the environment, mounts the Pane and prints its drawn tree. The child gets TZ=UTC and the
C.UTF-8 locale and none of this session's CLAUDE_*/FOREMAN_* (shot.CLEAN), so nothing reads or writes Sampo's state.
The pane takes time only from the engine's clock and draws no random numbers, so the frozen clock is its seed. The
text is an approximate layout of the tree (columns stack, rows sit side by side, fixed heights clip); --json is the
tree itself, less the engine's callback ids (press.handle), which count up per mount and draw nothing. Nothing from
the view reaches the terminal raw: the text drops escape sequences, control and bidi characters as the pane's own
clean() does, and --json and the diffs escape everything outside ASCII.
"""
import argparse
import base64
import difflib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot import CLEAN  # noqa: E402

MOD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = "FM-REPLAY "
PINNED = {"TZ": "UTC", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
OTHER = {"TZ": "Pacific/Kiritimati", "LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"}  # UTC+14, a decimal comma
TIMEOUT = 120  # s for one claude plugin test run (about 2 s here)
KIT = "replay.test.tsx"
# hooks/kit.ts clean() (T-0144): CSI (7- or 8-bit), the string forms OSC/DCS/SOS/PM/APC up to BEL or ST (or the end),
# any other ESC form; then C0/C1 controls (the newline kept, for the split), bidi overrides, zero-width and direction marks
ESC_SEQ = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|(?:\x1b[\]PX^_]|[\x90\x98\x9d-\x9f])[^\x07\x1b\x9c]*"
                     r"(?:\x07|\x1b\\|\x9c)?|\x1b[ -/]*[0-~]")
UNSAFE = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean(s):
    """s safe to print: no escape sequences, control or bidi characters but the newline; tabs as two spaces."""
    return UNSAFE.sub("", ESC_SEQ.sub("", s).replace("\t", "  "))


def options(view, cols, rows):
    """The temporary copy's tests/fixtures/replay.ts. The view goes in as a JSON string that JSON.parse reads, never
    as a JS literal: a title holding a template literal, a closing tag or a __proto__ key stays data."""
    return (f"export default {{ view: JSON.parse({json.dumps(json.dumps(view, ensure_ascii=True))}), "
            f"columns: {int(cols)}, rows: {int(rows)}, print: true }}\n")


def _kit_only(folder, names):
    """copytree's ignore: no tools and no test file but the replay one."""
    return [n for n in names if n == "tools" or (n.endswith((".test.ts", ".test.tsx")) and n != KIT)]


def stage(view, cols, rows, folder):
    """The mod copied into folder, ready for claude plugin test: replay.test.tsx its only test, drawing view at
    cols x rows and printing the tree. The copy's path."""
    if not isinstance(view, dict):
        raise ValueError("a fixture is one `fm ui --json` view: a JSON object")
    mod = os.path.join(folder, "foreman-ui")
    shutil.copytree(MOD, mod, ignore=_kit_only)
    with open(os.path.join(mod, "tests", "fixtures", "replay.ts"), "w", encoding="utf-8") as f:
        f.write(options(view, cols, rows))
    with open(os.path.join(mod, "tests", "fixtures", "golden.ts"), "w", encoding="utf-8") as f:
        f.write('export default ""\n')  # T-1670 review: the copy draws in print mode; a missing or conflicted golden still imports
    return mod


def drawn(view, cols=80, rows=40, env=None):
    """The pane's drawn tree for view, as the one JSON line the kit printed (byte-exact). claude runs in a process
    group of its own, killed whole past TIMEOUT: the runtime and the test it starts end with it."""
    with tempfile.TemporaryDirectory(prefix="fm-replay-") as tmp:
        with subprocess.Popen(["claude", "plugin", "test", stage(view, cols, rows, tmp)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                              env={**CLEAN, **(env or PINNED)}, cwd=tmp, start_new_session=True) as p:
            try:
                stdout, stderr = p.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
                raise
    out = [ln.rstrip("\r") for ln in (stdout + "\n" + stderr).split("\n")]  # splitlines breaks on U+2028 in a title
    line = next((ln for ln in out if ln.startswith(MARK)), None)
    if p.returncode or line is None:
        why = clean("\n".join(ln for ln in out if not ln.startswith(MARK))[-2000:])  # the failure, not the 30 KB tree
        raise RuntimeError(f"claude plugin test failed (exit {p.returncode}):\n{why}")
    return line[len(MARK):]


def _wide(c):
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def width(s):
    return sum(_wide(c) for c in s)


def _cut(s, w):
    out, n = [], 0
    for c in s:
        n += _wide(c)
        if n > w:
            break
        out.append(c)
    return "".join(out)


def _pad(s, w):
    return s + " " * (w - width(s))


def _words(node):
    if isinstance(node, str):
        return clean(node)
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return str(node)
    if isinstance(node, dict):
        return "".join(_words(k) for k in node.get("children") or [])
    if isinstance(node, list):
        return "".join(_words(k) for k in node)
    return ""


def _raster(p, w):
    raw = base64.b64decode(p.get("cells") or "")
    cps = [struct.unpack_from("<I", raw, i)[0] for i in range(0, len(raw) - 11, 12)]
    cols = int(p.get("columns") or 0) or len(cps) or 1
    ch = [" " if c >= 0x110000 or 0xD800 <= c < 0xE000 or UNSAFE.match(chr(c)) else chr(c) for c in cps]
    return [_cut("".join(ch[i:i + cols]), w) for i in range(0, len(ch), cols)]


def _block(node, w):
    """node as lines at most w columns wide."""
    if w <= 0:
        return []
    if not isinstance(node, dict) or node.get("type") == "Text":
        s = _words(node)
        return [_cut(ln, w) for ln in s.split("\n")] if s else []
    kind, p = node.get("type"), node.get("props") or {}
    if kind == "Raster":
        return _raster(p, w)
    if kind == "Button":
        return [_cut(clean(str(p.get("label") or "")), w)]
    if kind == "Input":
        return [_cut(clean(str(p.get("value") or p.get("placeholder") or "")), w)]
    if isinstance(p.get("width"), int):
        w = min(w, p["width"])
    right = int(p.get("paddingX") or 0)
    left = right + int(p.get("paddingLeft") or 0)
    inner, gap = w - left - right, int(p.get("gap") or 0)
    # ponytail: no flex growth, shrink or justification; the drawn tree (--json) is the exact render
    if p.get("flexDirection") == "row":
        blocks, used = [], 0
        for k in node.get("children") or []:
            b = _block(k, inner - used)
            fixed = (k.get("props") or {}).get("width") if isinstance(k, dict) else None
            bw = max([width(ln) for ln in b] + ([min(fixed, inner - used)] if isinstance(fixed, int) else []), default=0)
            if b or bw:
                blocks.append((b, bw))
                used += bw + gap
        tall = max((len(b) for b, _ in blocks), default=0)
        lines = [(" " * gap).join(_pad(b[i] if i < len(b) else "", bw) for b, bw in blocks) for i in range(tall)]
    else:
        lines = []
        for k in node.get("children") or []:
            b = _block(k, inner)
            if b and lines and gap:
                lines += [""] * gap
            lines += b
    if isinstance(p.get("height"), int):
        lines = (lines + [""] * p["height"])[:p["height"]]
    return [(" " * left + ln).rstrip() if ln.strip() else "" for ln in lines]


def text(tree, cols, rows):
    """The drawn tree as rows lines of at most cols columns, each ending in a newline: columns stack, rows sit side by
    side, fixed heights clip."""
    lines = (_block(tree, cols) + [""] * rows)[:rows]
    return "".join(ln.rstrip() + "\n" for ln in lines)


def load(path):
    """The view a fixture holds: a .json file, or a .ts module whose text after the line-starting `export default` is
    JSON (a title can't hold that: JSON escapes its newlines)."""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    if path.endswith(".ts"):
        m = re.search(r"^export default\b", raw, re.M)
        if not m:
            raise ValueError(f"{path}: no `export default` followed by the view's JSON")
        raw = raw[m.end():].strip().removesuffix(";")
    try:
        return json.loads(raw)
    except ValueError as e:
        raise ValueError(f"{path}: not a JSON view ({e})") from None


def twin(path, cols=80, rows=40, matrix=False):
    """[(label, drawn tree line, text)] for the fixture at path: two runs pinned to UTC and C.UTF-8, each its own
    process; matrix adds one in UTC+14 and a German locale."""
    view = load(path)
    runs = [("run 1", PINNED), ("run 2", PINNED)]
    if matrix:
        runs.append(("TZ=Pacific/Kiritimati LANG=de_DE.UTF-8", OTHER))
    out = []
    for label, env in runs:
        line = drawn(view, cols, rows, env)
        out.append((label, line, text(json.loads(line), cols, rows)))
    return out


GOLDEN = os.path.join(MOD, "tests", "fixtures", "golden.ts")  # T-1670
GOLDEN_VIEW, GOLDEN_SIZE = os.path.join(MOD, "tests", "fixtures", "pane-busy.ts"), (80, 40)  # fixtures/replay.ts's


def read_golden():
    """The committed golden frame (the drawn tree line), or None when there is none."""
    try:
        with open(GOLDEN, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    m = re.search(r"^export default\b", raw, re.M)
    return json.loads(raw[m.end():].strip().removesuffix(";")) if m else None


def golden(update=False):
    """(whether the pane draws the golden frame, a diff when it doesn't): the synthetic fixture at replay.ts's size, as
    the kit's replay test draws it. update: written, and the same; none committed: not the same, and nothing written."""
    line = drawn(load(GOLDEN_VIEW), *GOLDEN_SIZE)
    old = None if update else read_golden()  # update never reads it: a conflicted golden is what it rewrites
    if old is None and not update:
        return False, [f"no golden at {GOLDEN}; golden --update writes it\n"]
    if update:
        with open(GOLDEN, "w", encoding="utf-8") as f:
            f.write("// T-1670: the pane drawn from pane-busy.ts at 80x40 (fixtures/replay.ts), as tests/replay.test.tsx\n"
                    "// draws it. Written by tools/pane_replay.py golden --update after a change that draws differently.\n"
                    f"export default {json.dumps(line, ensure_ascii=True)}\n")
        return True, []
    if line == old:
        return True, []
    return False, diff([("golden", old, text(json.loads(old), *GOLDEN_SIZE)),  # text rows first: rasters are 30 KB lines
                        ("drawn now", line, text(json.loads(line), *GOLDEN_SIZE))])


def svg(frame, cols, rows):
    """A text frame as an SVG: its lines in monospace on the pane's dark ground, every character escaped."""
    import html
    cw, lh, pad = 8.4, 18, 12
    w, h = round(cols * cw + 2 * pad), rows * lh + 2 * pad
    lines = "".join(f'<text x="{pad}" y="{pad + (i + 1) * lh - 5}">{html.escape(ln)}</text>'
                    for i, ln in enumerate(frame.split("\n")) if ln.strip())  # T-2048: U+2028 in a title is no row
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'font-family="DejaVu Sans Mono, Menlo, Consolas, monospace" font-size="14"><rect width="{w}" height="{h}" '
            f'fill="#0d1420"/><g fill="#e7ecf4" xml:space="preserve">{lines}</g></svg>\n')


def write_svg(path, frame, cols, rows):
    """frame to path: an SVG, or a PNG through rsvg-convert for a path ending .png."""
    text_, ext = svg(frame, cols, rows), os.path.splitext(path)[1].lower()
    if ext not in (".svg", ".png"):
        raise ValueError(f"--out takes a .svg or .png file, not {path}")
    if ext == ".svg":
        with open(path, "w", encoding="utf-8") as f:
            f.write(text_)
        return
    if not shutil.which("rsvg-convert"):
        raise RuntimeError("a PNG needs rsvg-convert (librsvg), which isn't installed; --out FILE.svg works")
    try:
        subprocess.run(["rsvg-convert", "-o", path], input=text_.encode(), check=True, timeout=60, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"rsvg-convert failed: {clean(e.stderr.decode(errors='replace'))[-500:]}") from None


def size(lo, hi):
    def parse(s):
        n = int(s)
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"between {lo} and {hi}")
        return n
    return parse


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("render", "twin", "svg"):
        s = sub.add_parser(name)
        s.add_argument("fixture")
        s.add_argument("--cols", type=size(20, 400), default=80)
        s.add_argument("--rows", type=size(4, 200), default=40)
    sub.choices["render"].add_argument("--json", action="store_true", help="the drawn tree, not the text")
    sub.choices["twin"].add_argument("--matrix", action="store_true", help="a third run in another zone and locale")
    sub.choices["svg"].add_argument("--out", required=True, help="the file: .svg, or .png through rsvg-convert")
    sub.add_parser("golden").add_argument("--update", action="store_true", help="rewrite tests/fixtures/golden.ts")
    a = ap.parse_args()
    try:
        if a.cmd == "golden":
            same, lines = golden(a.update)
            sys.stdout.writelines(lines)
            print("golden: " + ("written" if a.update else "the pane draws it" if same else "none committed" if
                                lines[:1] and lines[0].startswith("no golden") else
                                "the pane drew differently; a change meant to draw so runs golden --update"))
            return 0 if same else 1
        if a.cmd == "svg":
            write_svg(a.out, text(json.loads(drawn(load(a.fixture), a.cols, a.rows)), a.cols, a.rows), a.cols, a.rows)
            print(f"wrote {a.out}")
            return 0
        if a.cmd == "render":
            tree = json.loads(drawn(load(a.fixture), a.cols, a.rows))
            # JSON.stringify leaves C1 controls and bidi raw: printed again, everything outside ASCII escaped
            sys.stdout.write(json.dumps(tree, separators=(",", ":")) + "\n" if a.json else text(tree, a.cols, a.rows))
            return 0
        runs = twin(a.fixture, a.cols, a.rows, a.matrix)
    except FileNotFoundError as e:
        sys.exit(f"pane_replay: {e.filename} not found"
                 + (": needs Claude Code (claude on PATH)" if e.filename == "claude" else ""))
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        sys.exit(f"pane_replay: {e}")
    lines = diff(runs)
    sys.stdout.writelines(lines)
    print(f"{'differ' if lines else 'identical'}: {', '.join(r[0] for r in runs)} ({a.cols}x{a.rows})")
    return 1 if lines else 0


def diff(runs):
    """A unified diff of every run against the first, empty when all drew the same bytes: the text frames when they
    differ, else the trees (a colour or a key the text doesn't show)."""
    (first, tree, frame), out = runs[0], []
    for label, line, txt in runs[1:]:
        if line != tree:
            a, b = (frame, txt) if txt != frame else (json.dumps(json.loads(tree), indent=1) + "\n",
                                                     json.dumps(json.loads(line), indent=1) + "\n")
            out += difflib.unified_diff(a.splitlines(True), b.splitlines(True), first, label)
    return out


if __name__ == "__main__":
    sys.exit(main())
