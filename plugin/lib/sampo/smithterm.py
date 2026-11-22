"""T-1300: the terminal under fm code tui, without curses. A Term turns a canvas of styled cells into ANSI output in the
colour the terminal can show (24-bit, the nearest of 256, 8, or none on NO_COLOR) and, after the first frame, writes
only the cells that changed. session() owns the terminal while the screen runs: the alternate screen, keys read raw
and parsed to the names the App binds, and every mode put back however it ends. Stdlib only."""
import functools
import os
import re
import select
import signal
import sys
import termios
import time
import tty

from sampo import killsafe  # os only

# what a terminal sends for a key → the name the App's keys use (curses' names, so tui.toml stays as it was)
SEQS = {"\x1b[A": "KEY_UP", "\x1b[B": "KEY_DOWN", "\x1b[C": "KEY_RIGHT", "\x1b[D": "KEY_LEFT", "\x1bOA": "KEY_UP",
        "\x1bOB": "KEY_DOWN", "\x1bOC": "KEY_RIGHT", "\x1bOD": "KEY_LEFT", "\x1b[5~": "KEY_PPAGE", "\x1b[6~": "KEY_NPAGE",
        "\x1b[H": "KEY_HOME", "\x1b[F": "KEY_END", "\x1b[1~": "KEY_HOME", "\x1b[4~": "KEY_END", "\x1bOH": "KEY_HOME",
        "\x1bOF": "KEY_END", "\x1b[3~": "KEY_DC", "\x1b[Z": "KEY_BTAB", "\x1bOP": "KEY_F(1)", "\x1bOQ": "KEY_F(2)",
        "\x1bOR": "KEY_F(3)", "\x1bOS": "KEY_F(4)"}
SEQS.update({f"\x1b[{n}~": f"KEY_F({i})" for i, n in zip(range(5, 13), (15, 17, 18, 19, 20, 21, 23, 24))})
# wrap off (?7l): a character in the last column can never scroll the screen; bracketed paste on (?2004h, T-1316)
ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?2004h\x1b[2J"
LEAVE = "\x1b[0m\x1b[?2004l\x1b[?7h\x1b[?25h\x1b[?1049l"
_LONGEST_FIRST = sorted(SEQS, key=len, reverse=True)
# a paste arrives between these as one ("paste", text) event; past PASTE_CAP characters the rest is dropped
PASTE_START, PASTE_END, PASTE_CAP = "\x1b[200~", "\x1b[201~", 64 * 1024
PASTE_STALL = 1.0  # seconds with no input before a paste whose end never came is taken as it stands


def colour_depth(env=None):
    """24, 256, 8 or 0 (NO_COLOR) from the environment a terminal sets."""
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return 0
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 24
    return 256 if "256" in env.get("TERM", "") else 8


def parse_keys(data):
    """Raw terminal input → key names: escape sequences as KEY_UP and the like, control keys as ^R, Enter as \\n."""
    out, i = [], 0
    while i < len(data):
        if data.startswith(PASTE_START, i):  # a paste: one event, never keys (an unended one runs to the data's end)
            end = data.find(PASTE_END, i)
            text = data[i + len(PASTE_START):end if end >= 0 else len(data)]
            out.append(("paste", text.replace("\r\n", "\n").replace("\r", "\n")[:PASTE_CAP]))
            i = end + len(PASTE_END) if end >= 0 else len(data)
            continue
        seq = next((s for s in _LONGEST_FIRST if data.startswith(s, i)), None)
        if seq:
            out.append(SEQS[seq])
            i += len(seq)
            continue
        ch = data[i]
        if ch == "\x1b" and i + 1 < len(data) and data[i + 1] in "[O":  # a sequence this table doesn't name: skip it
            j = i + 2
            while j < len(data) and not 0x40 <= ord(data[j]) <= 0x7E:
                j += 1
            i = j + 1
            continue
        out.append("KEY_BACKSPACE" if ch in "\x7f\b" else "\n" if ch == "\r" else
                   "^" + chr(ord(ch) + 64) if ord(ch) < 32 and ch not in "\t\n\x1b" else ch)
        i += 1
    return out


def split_tail(data):
    """(data, tail): an escape sequence cut off at the end of a read waits for the rest of it in the next, and so does
    a paste not yet ended; what a paste holds back is cut to PASTE_CAP (plus its last few characters, which may be
    the start of its end) so a paste can never grow the buffer without bound."""
    i = data.rfind(PASTE_START)
    if i >= 0 and data.find(PASTE_END, i) < 0:
        rest, head = data[i:], len(PASTE_START) + PASTE_CAP
        if len(rest) > head + len(PASTE_END):
            rest = rest[:head] + rest[-(len(PASTE_END) - 1):]
        return data[:i], rest
    i = data.rfind("\x1b")
    rest = data[i:] if i >= 0 else ""
    if rest and (rest in ("\x1b", "\x1bO") or rest.startswith("\x1b[") and all(0x20 <= ord(c) <= 0x3F for c in rest[2:])):
        return data[:i], rest
    return data, ""


_CPR = re.compile(r"\x1b\[\d+;(\d+)R")  # the terminal's answer to CSI 6n: where the cursor is


def probe(fd_in, fd_out, glyphs, timeout=0.25):
    """T-1512: the columns this terminal draws each glyph in: each written at the top-left corner, then the cursor's
    column asked for. ({glyph: columns}, the rest of what was read: keys typed meanwhile). Writes nothing unless both
    ends are a terminal (never into a pipe, a file or a test's capture); `timeout` bounds the whole probe, and a
    terminal that doesn't answer in it leaves the rest unmeasured. ponytail: Shift-F3 typed in that window reads as
    an answer (it is the same sequence); a wrong width there only picks the flat track."""
    if not (os.isatty(fd_in) and os.isatty(fd_out)):
        return {}, ""
    got, buf, end = {}, "", time.monotonic() + timeout
    try:
        for g in glyphs:
            os.write(fd_out, f"\x1b[1;1H{g}\x1b[6n".encode("utf-8"))
            while not (m := _CPR.search(buf)):
                left = end - time.monotonic()
                if left <= 0 or not select.select([fd_in], [], [], left)[0]:
                    return got, buf
                chunk = os.read(fd_in, 1024)
                if not chunk:
                    return got, buf
                buf += chunk.decode("utf-8", "replace")
            got[g], buf = int(m.group(1)) - 1, buf[:m.start()] + buf[m.end():]
    except OSError:  # the terminal went away mid-probe
        pass
    return got, buf


def _rgb(c):
    if isinstance(c, str) and len(c) == 7 and c.startswith("#"):
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    return None


BASE8 = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188), (17, 168, 205),
         (229, 229, 229)]


@functools.lru_cache(maxsize=4096)
def _code(c, depth, ground):
    """The SGR parameters for colour c (#rrggbb or a 0-255 index) on the foreground (3) or background (4)."""
    if c is None or depth == 0:
        return ""
    rgb = _rgb(c)
    if depth == 24 and rgb:
        return f";{ground}8;2;{rgb[0]};{rgb[1]};{rgb[2]}"
    if depth == 256 or (depth == 24 and not rgb):
        from sampo import smithtui
        return f";{ground}8;5;{smithtui.to_256(c)}"
    from sampo import smithtui
    r, g, b = smithtui._rgb(c)
    i = min(range(8), key=lambda k: sum((x - y) ** 2 for x, y in zip(BASE8[k], (r, g, b))))
    return f";{ground}{i}"


class Term:
    """Canvas → ANSI text. The first frame (and the first after a resize) writes every cell; later ones only the
    cells whose character or look changed."""

    def __init__(self, depth):
        self.depth, self.prev = depth, None

    def cells(self, cv, cfg):
        from sampo import smithtui
        screen = smithtui.resolve("screen", cfg)
        rows, memo = [], {}
        for y, row in enumerate(cv.cells):
            out = []
            for x, (ch, style) in enumerate(row):
                key = (style, cv.bg[y][x])
                if key not in memo:
                    memo[key] = self._sgr(style, cv.bg[y][x], cfg, screen)
                out.append((ch, memo[key]))
            rows.append(out)
        return rows

    def _sgr(self, style, under, cfg, screen):
        from sampo import smithtui
        s = smithtui.resolve(style, cfg, under)
        fg, bg = s.get("fg", screen.get("fg")), s.get("bg", screen.get("bg"))
        rev = bool(s.get("reverse"))
        if self.depth == 0:  # no colour: a chip or a selection still shows, reversed (its own background, not a panel's)
            own = smithtui.resolve(style, cfg)
            rev = rev or (own.get("bg") is not None and style not in ("screen", "text", "header", "header_dim", "bar",
                                                                      "bar_dim"))
            fg = bg = None
        return "\x1b[0" + (";1" if s.get("bold") else "") + (";3" if s.get("italic") else "") + (
            ";7" if rev else "") + _code(fg, self.depth, 3) + _code(bg, self.depth, 4) + "m"

    def frame(self, cv, cfg):
        rows, prev, out = self.cells(cv, cfg), self.prev, []
        same_size = prev is not None and len(prev) == len(rows) and all(len(a) == len(b) for a, b in zip(prev, rows))
        cur = None
        for y, row in enumerate(rows):
            x = 0
            while x < len(row):
                if same_size and row[x] == prev[y][x]:
                    x += 1
                    continue
                if row[x][0] == "" and x:  # the right half of a wide character: write it from its left half
                    x -= 1
                out.append(f"\x1b[{y + 1};{x + 1}H")
                while x < len(row) and not (same_size and row[x] == prev[y][x]):
                    ch, sgr = row[x]
                    if sgr != cur:
                        out.append(sgr)
                        cur = sgr
                    out.append(ch)
                    x += 1
        self.prev = rows
        return "".join(out)


def session(fn, fd_in=None, fd_out=None):
    """Own the terminal for fn(io): the alternate screen, keys without echo or line buffering (Ctrl-C still
    interrupts), and everything put back on any exit (a crash, Ctrl-C, a closed terminal). Ctrl-Z gives the terminal
    back while stopped and takes it again on fg. io has write(text), size(), keys(timeout) and resized()."""
    fd_in = sys.stdin.fileno() if fd_in is None else fd_in
    fd_out = sys.stdout.fileno() if fd_out is None else fd_out
    old = termios.tcgetattr(fd_in)
    flags = {"resized": True, "pending": "", "read_at": 0.0}
    wake_r, wake_w = os.pipe()  # a signal wakes the wait for keys: select is retried after a handler (PEP 475)
    os.set_blocking(wake_w, False)

    def wake():
        try:
            os.write(wake_w, b"!")
        except OSError:
            pass

    class IO:
        def write(self, text):
            data = text.encode("utf-8")
            while data:
                data = data[os.write(fd_out, data):]

        def size(self):
            try:
                s = os.get_terminal_size(fd_out)
                return s.columns, s.lines
            except OSError:
                return 80, 24

        def keys(self, timeout):
            pasting = flags["pending"].startswith(PASTE_START)
            if flags["pending"] and not pasting:  # a lone Esc, or a sequence cut off: wait briefly for its rest
                timeout = min(timeout, 0.05)
            r, _, _ = select.select([fd_in, wake_r], [], [], timeout)
            if wake_r in r:
                os.read(wake_r, 64)
            if fd_in not in r:
                if pasting and time.monotonic() - flags["read_at"] < PASTE_STALL:  # a paste's rest is still coming
                    return []
                data, flags["pending"] = flags["pending"], ""
                return parse_keys(data) if data else []
            try:
                data = flags["pending"] + os.read(fd_in, 4096).decode("utf-8", "replace")
            except OSError:
                return []
            flags["read_at"] = time.monotonic()
            data, flags["pending"] = split_tail(data)
            return parse_keys(data)

        def resized(self):
            was, flags["resized"] = flags["resized"], False
            return was

        def outside(self, fn):
            """T-1341: fn with the terminal handed back (a program the person drives in it), then taken again and
            redrawn whole."""
            restore()
            try:
                return fn()
            finally:
                try:
                    tty.setcbreak(fd_in)
                    io.write(ENTER)
                except (OSError, termios.error):
                    pass
                winch()

        def widths(self, glyphs):  # T-1512: before the first frame, which clears what the probe drew
            got, typed = probe(fd_in, fd_out, glyphs)
            flags["pending"] += typed  # read with the next keys
            return got

    io = IO()

    def restore():
        try:
            io.write(LEAVE)
        except OSError:  # the terminal is gone (SIGHUP): nothing to put back on it
            pass
        try:
            termios.tcsetattr(fd_in, termios.TCSADRAIN, old)
        except (OSError, termios.error):
            pass

    def winch(*_):
        flags["resized"] = True
        wake()

    def suspend(*_):  # Ctrl-Z: the shell gets its terminal back; fg takes it again and redraws everything
        restore()
        # killsafe refuses pid <= 1, so as pid 1 (a container's init) there is no SIGSTOP to ourselves: Ctrl-Z does nothing
        killsafe.kill(os.getpid(), signal.SIGSTOP)
        try:
            tty.setcbreak(fd_in)
            io.write(ENTER)
        except (OSError, termios.error):
            pass
        winch()

    prev = {}
    for name, handler in (("SIGWINCH", winch), ("SIGTSTP", suspend)):
        if hasattr(signal, name):
            prev[name] = signal.signal(getattr(signal, name), handler)
    try:
        tty.setcbreak(fd_in)
        io.write(ENTER)
        return fn(io)
    finally:
        restore()
        for name, handler in prev.items():
            signal.signal(getattr(signal, name), handler)
        os.close(wake_r)
        os.close(wake_w)
