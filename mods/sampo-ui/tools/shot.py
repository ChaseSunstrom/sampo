#!/usr/bin/env python3
"""foreman-ui snapshot tool (T-0131): run Claude Code in a detached tmux window with this mod loaded from its folder and
draw the screen, colours included, to a PNG, so a UI change can be looked at and not only tested.

  shot.py start DIR [--cols 200] [--rows 60] [--model haiku]
                                               claude --plugin-dir <this mod> in DIR (FOREMAN_STATE passes through);
                                               --view FIXTURE: the mod draws that recorded view (T-3701)
  shot.py keys TEXT [--enter]                  type into it: /fm, /reload-plugins, a digit for a dialog
  shot.py snap OUT.png                         the visible screen as a PNG (and OUT.txt, its plain text)
  shot.py self OUT.png [--back N]              the same for the tmux pane this session runs in (its own UI, live)
  shot.py watch OUT.png [--every 5] [--for 7200]  self, again every few seconds (run it detached: setsid nohup … &)
  shot.py stop
  shot.py selftest                             checks the harness can't leak FOREMAN_* into anyone's tmux

It runs on a tmux server of its own (-L fm-shot), started without FOREMAN_* in its environment: a session's
FOREMAN_STATE is that session's alone (T-0138 fix: the first version started the user's default tmux server from a
shell that had FOREMAN_STATE set, and the server's global environment handed it to every later session).

Local commands (/fm, /reload-plugins) send the model nothing; a typed prompt is a paid turn, so pair it with --model haiku.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata

SESSION = "fmui"
SOCKET = os.environ.get("FM_SHOT_SOCKET", "fm-shot")  # T-0744: a test run uses its own, so two never collide
# T-0903: nor this session's Claude Code identity (CLAUDECODE, CLAUDE_CODE_SESSION_ID, its messaging socket…): the
# harnessed Claude Code is a session of its own, not this one's child
CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("FOREMAN_", "CLAUDE_", "CLAUDECODE")) and k != "TMUX"}
MOD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FG, BG = (208, 208, 208), (24, 26, 33)  # the terminal's default colours


def tmux(*args, check=True):
    return subprocess.run(["tmux", "-L", SOCKET, *args], capture_output=True, text=True, check=check, env=CLEAN)


def start(a, cmd=None):
    tmux("kill-session", "-t", SESSION, check=False)
    # T-0995: on the user's own state a one-off: the drive never pushes it into their queue (it once planned three tasks
    # and took the focus). T-1013: on a state of its own (the demo recorder) it is that state's working session
    own = os.environ.get("FOREMAN_STATE")
    # T-1209: Claude Code drops to 256 colours inside tmux unless told the server passes true colour through
    env = ["-e", "COLORTERM=truecolor", "-e", "CLAUDE_CODE_TMUX_TRUECOLOR=1",
           "-e", f"FOREMAN_STATE={own}" if own else "FOREMAN_ONEOFF=1"]
    if getattr(a, "view", None):  # T-3701: the mod draws a recorded view: FOREMAN_FM is a stub that answers `ui` with it
        env += ["-e", f"FOREMAN_FM={stub_fm(a.view)}"]
    elif getattr(a, "fm", None):  # T-4008: the demo recorder's view from its own checkout's Sampo, not the installed one
        env += ["-e", f"FOREMAN_FM={a.fm}"]
    # T-2055: settings = a JSON string for --settings (the demo recorder turns off every non-Sampo plugin with it)
    tmux("new-session", "-d", "-s", SESSION, "-x", str(a.cols), "-y", str(a.rows), "-c", os.path.abspath(a.dir), *env,
         cmd or f"claude --plugin-dir {MOD}" + (f" --model {a.model}" if getattr(a, "model", None) else "")
         + (f" --settings {shlex.quote(a.settings)}" if getattr(a, "settings", None) else ""))
    print(f"started {SESSION} ({a.cols}x{a.rows}) in {a.dir}")
    if cmd is None and answer_trust(a.dir):
        print("answered Claude Code's folder-trust prompt (a folder Sampo made)")


def stub_fm(fixture):
    """T-3701: an fm for the harnessed mod that prints fixture's view (a .json file, or a tests/fixtures .ts one) for
    `fm ui …` and `ok` for anything else, so a screenshot shows a chosen view and no command touches real state."""
    import json
    from pane_replay import load
    d = tempfile.mkdtemp(prefix="fm-shot-view-")
    with open(os.path.join(d, "view.json"), "w", encoding="utf-8") as f:
        json.dump(load(fixture), f)
    path = os.path.join(d, "fm")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\n[ "$1" = ui ] && exec cat {shlex.quote(os.path.join(d, "view.json"))}\necho ok\n')
    os.chmod(path, 0o755)
    return path


def _ours(path):
    """T-1032: Sampo's own repo, or a scratch folder under the temp folder that carries no Claude Code project
    config of its own (.claude/, .mcp.json, CLAUDE.md, here or above it up to the temp folder): trusting a folder is
    what lets its config run hooks and servers, so a repo cloned into /tmp with any of that is never answered for
    (review: all of /tmp was too broad)."""
    real = os.path.realpath(path)
    repo = os.path.realpath(os.path.dirname(os.path.dirname(MOD)))
    if real == repo or real.startswith(repo + os.sep):
        return True
    tmp = os.path.realpath(tempfile.gettempdir())
    if not real.startswith(tmp + os.sep) or not os.path.isdir(real):
        return False
    d = real
    while d.startswith(tmp + os.sep):
        if any(os.path.exists(os.path.join(d, x)) for x in (".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md")):
            return False
        d = os.path.dirname(d)
    return True


def answer_trust(path, wait=12):
    """T-1032: Claude Code asks whether to trust a folder it hasn't seen, and the harness stalled there on every new
    folder (each demo recording). It picks "Yes, I trust this folder" itself, but only for a folder Sampo made:
    trusting a cloned repo would let that repo's own hooks run. True when it answered."""
    if not _ours(path):
        return False
    end = time.time() + wait
    while time.time() < end:
        screen = tmux("capture-pane", "-p", "-t", SESSION, check=False).stdout or ""
        if "trust this folder" in screen:
            tmux("send-keys", "-t", SESSION, "Down")
            tmux("send-keys", "-t", SESSION, "Enter")
            return True
        if re.search(r"(?m)^\s*❯\s*$|─{20,}\n\s*❯", screen):  # the prompt is up: nothing to answer
            return False
        time.sleep(0.5)
    return False


def keys(a):
    tmux("send-keys", "-t", SESSION, "-l", a.text)
    if a.enter:
        tmux("send-keys", "-t", SESSION, "Enter")


def _palette(n):
    base = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188), (17, 168, 205),
            (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67), (59, 142, 234),
            (214, 112, 214), (41, 184, 219), (255, 255, 255)]
    if n < 16:
        return base[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return steps[n // 36], steps[n // 6 % 6], steps[n % 6]
    v = 8 + (n - 232) * 10
    return v, v, v


_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")  # hyperlinks and titles: not drawn
FALLBACK = ["/usr/share/fonts/noto/NotoSansSymbols2-Regular.ttf", "/usr/share/fonts/noto/NotoSansMono-Regular.ttf",
            "/usr/share/fonts/noto/NotoSansSymbols-Regular.ttf", "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/noto/NotoSansRunic-Regular.ttf"]  # T-3113: the crest's rune ring (Konsole falls back to it)


def cells(line):
    """[(char, fg, bg, bold, width)] for one captured line."""
    out, fg, bg, bold, rev, dim = [], None, None, False, False, False
    pos = 0
    for m in list(_SGR.finditer(line)) + [None]:
        text = line[pos:m.start()] if m else line[pos:]
        for ch in text:
            if ch < " ":
                continue
            f, b = (bg or BG, fg or FG) if rev else (fg or FG, bg or BG)
            if dim:
                f = tuple((x + y) // 2 for x, y in zip(f, b))
            out.append((ch, f, b, bold, 2 if unicodedata.east_asian_width(ch) in "WF" else 1))
        if not m:
            break
        pos = m.end()
        nums = [int(x) if x else 0 for x in re.split(r"[;:]", m.group(1))] or [0]
        i = 0
        while i < len(nums):
            n = nums[i]
            if n == 0:
                fg, bg, bold, rev, dim = None, None, False, False, False
            elif n == 1:
                bold = True
            elif n == 2:
                dim = True
            elif n == 22:
                bold = dim = False
            elif n == 7:
                rev = True
            elif n == 27:
                rev = False
            elif n in (38, 48) and i + 1 < len(nums):
                if nums[i + 1] == 2 and i + 4 < len(nums):
                    rgb = tuple(nums[i + 2:i + 5])
                    i += 4
                elif nums[i + 1] == 5 and i + 2 < len(nums):
                    rgb = _palette(nums[i + 2])
                    i += 2
                else:
                    rgb = None
                if n == 38:
                    fg = rgb
                else:
                    bg = rgb
            elif n == 39:
                fg = None
            elif n == 49:
                bg = None
            elif 30 <= n <= 37 or 90 <= n <= 97:
                fg = _palette(n - 30 if n < 90 else n - 82)
            elif 40 <= n <= 47 or 100 <= n <= 107:
                bg = _palette(n - 40 if n < 100 else n - 92)
            i += 1
    return out


def snap(a):
    if a.cmd in ("self", "watch") and not os.environ.get("TMUX_PANE"):
        sys.exit("not inside tmux: snapshot the window instead (spectacle -b -n -a -o OUT.png)")
    if a.cmd in ("self", "watch"):  # the pane this Claude Code session runs in, on the user's own tmux server (read-only)
        raw = subprocess.run(["tmux", "capture-pane", "-p", "-e", "-t", os.environ["TMUX_PANE"], "-S", str(-a.back)],
                             capture_output=True, text=True, check=True).stdout
    else:
        raw = tmux("capture-pane", "-p", "-e", "-t", SESSION).stdout
    raw = _OSC.sub("", raw)
    with open(os.path.splitext(a.out)[0] + ".txt", "w") as f:
        f.write(_SGR.sub("", raw))
    rows = render(raw, a.out)
    print(f"{a.out} ({rows} rows)")


_FONTS = {}


def render(raw, out, cols=None, rows=None):
    """T-0903: a captured screen (SGR colours, no OSC) drawn to a PNG at out; its row count. cols and rows fix the
    size, so every frame of a recording is the same."""
    from PIL import Image, ImageDraw, ImageFont
    if not _FONTS:
        _FONTS.update(font=ImageFont.truetype("/usr/share/fonts/TTF/Hack-Regular.ttf", 15),
                      bold=ImageFont.truetype("/usr/share/fonts/TTF/Hack-Bold.ttf", 15), pick={},
                      fallbacks=[ImageFont.truetype(f, 15) for f in FALLBACK if os.path.exists(f)])
    font, bold, pick, fallbacks = _FONTS["font"], _FONTS["bold"], _FONTS["pick"], _FONTS["fallbacks"]
    height, rows = rows, raw.rstrip("\n").split("\n")
    if height:
        rows = (rows + [""] * height)[:height]
    cw, ch = round(font.getlength("M")), 19

    def sig(f, c):  # the pixels a font draws for c: a missing glyph draws the same box as a surely-missing one
        im = Image.new("L", (32, 32))
        ImageDraw.Draw(im).text((0, 0), c, font=f, fill=255)
        return im.tobytes()

    def face(c, b):  # Hack lacks ✓ ⚑ ◆ and friends: the first font that has the glyph draws it
        if c not in pick:
            pick[c] = next((f for f in fallbacks if sig(f, c) != sig(f, "\U0010FFFD")), None) \
                if sig(font, c) == sig(font, "\U0010FFFD") else None
        return pick[c] or (bold if b else font)
    cols = cols or (max(sum(c[4] for c in cells(r)) for r in rows) if rows else 80)
    img = Image.new("RGB", (max(cols, 1) * cw + 16, len(rows) * ch + 16), BG)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(rows):
        x = 0
        for c, fg, bg, b, w in cells(row):
            px, py = 8 + x * cw, 8 + y * ch
            if bg != BG:
                d.rectangle([px, py, px + w * cw - 1, py + ch - 1], fill=bg)
            if c == "█":
                d.rectangle([px, py, px + cw - 1, py + ch - 1], fill=fg)
            elif c in "▀▄":  # T-3210: drawn as a terminal does, an exact half: Hack's glyphs leave a seam
                top = py if c == "▀" else py + ch // 2
                d.rectangle([px, top, px + cw - 1, top + (ch // 2 if c == "▀" else ch - ch // 2) - 1], fill=fg)
            elif c != " ":
                d.text((px, py + 1), c, font=face(c, b), fill=fg)
            x += w
    img.save(out)
    return len(rows)


def watch(a):
    """T-0143: snapshot this session's own pane every few seconds until --for runs out. Start it detached
    (setsid nohup … &), never as a Claude Code background task: drive waits for those to finish."""
    import time
    end = time.time() + a.duration
    while True:
        snap(a)
        if time.time() + a.every > end:
            return
        time.sleep(a.every)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("dir")
    s.add_argument("--cols", type=int, default=200)
    s.add_argument("--rows", type=int, default=60)
    s.add_argument("--model", help="e.g. haiku: a cheap turn, without changing the saved default model as /model does")
    s.add_argument("--settings", help="JSON for claude --settings, e.g. '{\"enabledPlugins\": {\"sampo-ui@sampo\": "
                   "false}}' so a worktree's mod draws, not the installed one (T-3163)")
    s.add_argument("--view", help="a recorded `fm ui --json` view (.json, or a tests/fixtures .ts) the mod draws (T-3701)")
    s = sub.add_parser("keys")
    s.add_argument("text")
    s.add_argument("--enter", action="store_true")
    s = sub.add_parser("snap")
    s.add_argument("out")
    s = sub.add_parser("self")
    s.add_argument("out")
    s.add_argument("--back", type=int, default=0, help="also this many lines of scrollback above the screen")
    s = sub.add_parser("watch")
    s.add_argument("out")
    s.add_argument("--every", type=float, default=5)
    s.add_argument("--for", dest="duration", type=float, default=7200, help="seconds, then it stops")
    s.add_argument("--back", type=int, default=0)
    sub.add_parser("stop")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "stop":
        tmux("kill-session", "-t", SESSION, check=False)
    elif a.cmd == "selftest":
        start(argparse.Namespace(dir=".", cols=80, rows=24), cmd="sleep 30")
        server = tmux("show-environment", "-g").stdout
        session = tmux("show-environment", "-t", SESSION).stdout
        tmux("kill-server", check=False)
        if SOCKET != "fm-shot":  # a test's own socket: leave no file behind
            try:
                os.unlink(os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", SOCKET))
            except OSError:
                pass
        leaked = [ln for ln in server.splitlines() if ln.startswith("FOREMAN_")]
        if leaked:
            sys.exit(f"FAIL: the harness server's global environment has {leaked}")
        if os.environ.get("FOREMAN_STATE") and "FOREMAN_STATE=" not in session:
            sys.exit("FAIL: the harness session lost its FOREMAN_STATE")
        print("ok: FOREMAN_* only in the harness session, never the server")
    else:
        {"start": start, "keys": keys, "snap": snap, "self": snap, "watch": watch}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
