#!/usr/bin/env python3
"""Record `sampo demo --pace 1` for the README (T-0902): through a pseudo-terminal into demo/demo.cast (asciinema v2,
playable with `asciinema play demo/demo.cast`), then render demo/demo.svg (animated; GitHub plays it inline) with
svg-term-cli via npx. `--mp4` also makes demo/demo.mp4 for sharing elsewhere: svg-term stills every 1/FPS s, shot by
a headless Chromium (CHROME, or Playwright's), joined by ffmpeg. Re-run after the demo changes:
    python3 demo/record.py [--no-svg] [--mp4]"""
import fcntl
import glob
import json
import os
import shutil
import tempfile
import pty
import select
import struct
import subprocess
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FM = os.path.join(os.path.dirname(HERE), "plugin", "bin", "sampo")
COLS, ROWS = 100, 36
TYPED = "sampo demo --pace 1"  # T-3989: the name a user types; doctor.check_recordings flags a retired one


def record(path):
    events, t = [], 0.6
    events.append([t, "o", "\x1b[1;32m$\x1b[0m "])
    for ch in TYPED:  # the prompt, typed
        t += 0.06
        events.append([round(t, 3), "o", ch])
    t += 0.5
    events.append([round(t, 3), "o", "\r\n"])
    pid, fd = pty.fork()
    if pid == 0:  # the child: a terminal of COLS x ROWS running the real demo
        fcntl.ioctl(sys.stdout.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        os.environ.update(COLUMNS=str(COLS), LINES=str(ROWS), TERM="xterm-256color")
        os.chdir(tempfile.gettempdir())  # T-0919: outside any project, so it ends with the generic first step
        os.execv(sys.executable, [sys.executable, FM, "demo", "--pace", "1"])
    t0 = time.time() - t
    while True:
        r, _, _ = select.select([fd], [], [], 0.2)
        if fd in r:
            try:
                data = os.read(fd, 4096)
            except OSError:  # the child closed the terminal
                break
            if not data:
                break
            events.append([round(time.time() - t0, 3), "o", data.decode("utf-8", "replace")])
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status):
        sys.exit(f"sampo demo failed: {''.join(e[2] for e in events)[-500:]}")
    events.append([round(events[-1][0] + 4, 3), "o", ""])  # hold the last screen
    header = {"version": 2, "width": COLS, "height": ROWS, "timestamp": int(time.time()), "title": "sampo demo",
              "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"}}
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n" + "".join(json.dumps(e) + "\n" for e in events))
    return events[-1][0]


SVG_TERM = ["npx", "-y", "svg-term-cli@2.1.1", "--window", "--width", str(COLS), "--height", str(ROWS), "--padding", "12"]
FPS = 4


def mp4(cast, seconds, out):
    chrome = os.environ.get("CHROME") or next(iter(sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))), None)
    if not chrome or not shutil.which("ffmpeg"):
        sys.exit("--mp4 needs a headless Chromium (set CHROME) and ffmpeg")
    with tempfile.TemporaryDirectory(prefix="fm-demo-frames-") as t:
        for i in range(int(seconds * FPS) + 1):
            frame = os.path.join(t, f"{i:04d}")
            subprocess.run(SVG_TERM + ["--in", cast, "--out", frame + ".svg", "--at", str(i * 1000 // FPS)], check=True,
                           capture_output=True)
            subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                            "--window-size=1064,866", f"--screenshot={frame}.png", f"file://{frame}.svg"],
                           check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", os.path.join(t, "%04d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", out],
                       check=True)


if __name__ == "__main__":
    cast, svg = os.path.join(HERE, "demo.cast"), os.path.join(HERE, "demo.svg")
    seconds = record(cast)
    print(f"recorded {seconds:.0f} s → {cast}")
    if "--no-svg" not in sys.argv:
        subprocess.run(SVG_TERM + ["--in", cast, "--out", svg], check=True)
        print(f"rendered → {svg} ({os.path.getsize(svg) // 1024} KB)")
    if "--mp4" in sys.argv:
        mp4(cast, seconds, os.path.join(HERE, "demo.mp4"))
        print(f"video → {os.path.join(HERE, 'demo.mp4')} ({os.path.getsize(os.path.join(HERE, 'demo.mp4')) // 1024} KB)")
