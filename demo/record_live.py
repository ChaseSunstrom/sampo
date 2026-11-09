#!/usr/bin/env python3
"""Record Claude Code itself, with the foreman-ui mod, fixing a seeded bug through Sampo (T-0903): the band above the
prompt, the animated tool rows, the step bar and the status line, as a user sees them.

A throwaway repo (calc.add subtracts) and a Sampo state of its own (FOREMAN_STATE, so the task is T-0001 and nothing
of yours is touched) under /tmp/fm-live-<time>; Claude Code runs in shot.py's tmux harness (its own server) with the
mod from this checkout; the request is typed in, and the coloured screen is captured until the task is done. Writes
demo/live.cast (each capture a full redraw, waits squeezed to MAX_GAP s), demo/live.svg (svg-term-cli via npx; GitHub
plays it inline), demo/live.mp4 (shot.py's renderer + ffmpeg) and the README's still, docs/assets/screens/
claude-code.png (the last screen with a task in focus). Your home folder, user name and account e-mail are replaced
before anything is written. A real model turn: it costs a little usage (default --model sonnet).

    python3 demo/record_live.py [--model sonnet] [--max 600] [--no-svg]
    python3 demo/record_live.py --repo . --request "FIX: …" [--dry]   (T-0979: a real task in a clone of a repo)"""
import argparse
import getpass
import importlib.util
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location("shot", os.path.join(ROOT, "mods", "foreman-ui", "tools", "shot.py"))
shot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shot)

# T-4008: wide enough for a two-column dock (84+ inside) and tall enough for all five rows of its ten panels at
# 12-row pictures: the README shows the whole grid, not the two panels a 46-row dock has room for
COLS, ROWS = 200, 88
STILL = os.path.join(ROOT, "docs", "assets", "screens", "claude-code.png")
REQUEST = "FIX: calc.add(2, 3) returns -1 instead of 5"
BUG = "def add(a, b):\n    return a - b\n"
MAX_GAP = 1.2  # seconds a still screen is held at most (the model thinking), so the video stays watchable


def only_foreman(path=os.path.expanduser("~/.claude/settings.json")):
    """T-2055: --settings JSON that turns off every plugin the user enabled outside the sampo marketplace (foreman
    before T-3707; another plugin's hook warning or status line must not reach a recording); login, permissions and
    env stay. None when the settings file is missing or unreadable."""
    try:
        with open(path) as f:
            on = json.load(f).get("enabledPlugins", {})
        off = {k: False for k, v in on.items() if v is True and k.rsplit("@", 1)[-1] not in ("sampo", "foreman")}
    except (OSError, ValueError, AttributeError):
        return None
    return json.dumps({"enabledPlugins": off})


NOTICE = re.compile(r"Your login expires in|Your \w+ plan includes|clau\.de/api-credits|tmux focus-events off"
                    r"|hooks\.json: unknown keys|\w\" in hooks\.\w+\[\d+\]"
                    r"|You've used \d+% of your|hooks\.\w+\[\d+\] and \d+ more ignored")  # Claude Code's notices, not Sampo's;
# T-4008: the usage one names your time zone; the hooks one counts the hooks of the plugins only_foreman turned off
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # every CSI, not shot._SGR's colours alone: a cast event opens with \x1b[H\x1b[2J


def quiet(s, sep="\n"):
    """T-2055: blank each startup notice (an installed but disabled plugin's hooks.json warning still prints; the login
    expiry, plan promo and tmux tip are account or harness noise): its characters up to the Sampo panel's │ become
    spaces, so the panel beside it and the screen's layout hold."""
    out = []
    for x in s.split(sep):
        plain = ANSI.sub("", x)
        m = NOTICE.search(plain)
        if m:
            end = plain.find("│", m.end())
            end, i, parts = len(plain) if end < 0 else end, 0, []
            for tok in re.split(f"({ANSI.pattern})", x):
                if not tok.startswith("\x1b"):  # ponytail: one space per character; a wide one would shift the row
                    tok, i = "".join(" " if i + k < end else c for k, c in enumerate(tok)), i + len(tok)
                parts.append(tok)
            x = "".join(parts)
        out.append(x)
    return sep.join(out)


def scrubber():
    """What must not reach a published recording: the home folder, the user and real name, the account e-mail."""
    home, me = os.path.expanduser("~"), pwd.getpwuid(os.getuid())
    names = [getpass.getuser(), *re.split(r"[ ,]+", me.pw_gecos)]
    try:
        acct = json.load(open(os.path.join(home, ".claude.json"))).get("oauthAccount") or {}
        for k, v in acct.items():
            if isinstance(v, str) and "mail" in k.lower():
                names.append(v)
            elif isinstance(v, str) and "name" in k.lower():
                names += re.split(r"[ ,']+", v)
    except (OSError, ValueError):
        pass
    for key in ("user.email", "user.name"):  # T-0979: a session that commits may show git's author line
        got = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True).stdout.strip()
        names += [got] if "@" in got else re.split(r"[ ,']+", got)
    names = sorted({n for n in names if len(n) > 2}, key=len, reverse=True)
    # whole words only: a name inside another word ("purchase") is left alone
    pat = re.compile(r"(?<![\w.@-])(?:" + "|".join(map(re.escape, names)) + r")(?![\w@-])", re.I) if names else None

    def scrub(s):
        s = quiet(s).replace(home, "~")
        return pat.sub("you", s) if pat else s
    return scrub


def seed(work):
    repo = os.path.join(work, "calc")
    os.makedirs(repo)
    with open(os.path.join(repo, "calc.py"), "w") as f:
        f.write(BUG)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("# calc\n\nA tiny calculator. `add(a, b)` should return the sum.\n")
    with open(os.path.join(repo, ".gitignore"), "w") as f:  # T-4008: as a real repo has; else the close names a .pyc
        f.write("__pycache__/\n")
    for cmd in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.email=demo@example.com", "-c", "user.name=demo",
                                                            "commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", repo, *cmd], check=True)
    return repo


def clone(work, src):
    """T-0979: a real repo instead of the seeded one: a local clone of its HEAD (no hooks, no remotes, no history
    beyond what the session needs), so the session works a real task and nothing of the original is touched."""
    repo = os.path.join(work, os.path.basename(os.path.abspath(src)) or "repo")
    subprocess.run(["git", "clone", "-q", "--no-local", "--depth", "50", "file://" + os.path.abspath(src), repo],
                   check=True)
    subprocess.run(["git", "-C", repo, "remote", "remove", "origin"], check=True)
    return repo


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", help="record a session in a clone of this repo instead of the seeded calc one")
    ap.add_argument("--request", default=REQUEST, help="what is typed into the session")
    ap.add_argument("--dry", action="store_true", help="say what would be recorded; start nothing")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max", type=int, default=600, help="seconds to wait for the task to close")
    ap.add_argument("--no-svg", action="store_true")
    ap.add_argument("--from-cast", action="store_true", help="no new session: render demo/live.cast again")
    ap.add_argument("--max-frames", type=int, default=240, help="thin the recording to about this many screens "
                    "(evenly in time, the last kept), so the SVG stays small enough for a README")
    a = ap.parse_args()
    if a.from_cast:
        with open(os.path.join(HERE, "live.cast"), encoding="utf-8") as f:
            head, *events = [json.loads(ln) for ln in f]
        events = [e[:2] + [quiet(e[2], "\r\n")] + e[3:] for e in thin(kept(events), a.max_frames)]
        with open(os.path.join(HERE, "live.cast"), "w", encoding="utf-8") as f:
            f.write(json.dumps(head) + "\n" + "".join(json.dumps(e) + "\n" for e in events))
        return render(events, a)
    if a.dry:
        try:
            claude = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
        except OSError:
            claude = ""
        return print(f"would record: {'a clone of ' + os.path.abspath(a.repo) if a.repo else 'the seeded calc repo'}"
                     f"\nrequest: {a.request}\nclaude: {claude or 'not on PATH'} · model {a.model} · up to {a.max} s")
    work = tempfile.mkdtemp(prefix=f"fm-live-{int(time.time())}-", dir="/tmp")
    repo, scrub = (clone(work, a.repo) if a.repo else seed(work)), scrubber()
    os.environ["FOREMAN_STATE"] = os.path.join(work, "state")  # shot.start hands it to the session alone
    shot.CLEAN["DISABLE_AUTOUPDATER"] = "1"  # its banner ("Auto-update failed…") is this machine's, not the product's
    # T-4008: your account's weekly usage is yours, not the demo's: its economy toast and halved caps stay out
    subprocess.run([sys.executable, os.path.join(ROOT, "plugin", "bin", "sampo"), "drive", "on", "--pace", "off"], cwd=repo,
                   check=True, capture_output=True)
    shot.start(argparse.Namespace(dir=repo, cols=COLS, rows=ROWS, model=a.model, settings=only_foreman(),
                                  fm=os.path.join(ROOT, "plugin", "bin", "fm")))  # T-4008: the dock from this tree
    frames, t0 = [], time.time()

    def grab():
        raw = scrub(shot._OSC.sub("", shot.tmux("capture-pane", "-p", "-e", "-t", shot.SESSION, check=False).stdout))
        if raw.strip() and (not frames or frames[-1][1] != raw):
            frames.append((time.time() - t0, raw))
        return shot._SGR.sub("", raw)

    def done():  # T-0979: any task, not just T-0001: a session may drop its first brief and redo it as T-0002
        return any("status: done" in open(os.path.join(d, f)).read() for d, _, fs in os.walk(os.environ["FOREMAN_STATE"])
                   for f in fs if re.match(r"T-\d+-.*\.md$", f))
    try:
        for _ in range(60):  # the folder-trust question, then an idle prompt
            text = grab()
            if "trust this folder" in text.lower():  # T-1034: only a folder Sampo made, never a clone's own hooks
                if not shot.answer_trust(repo, wait=2):
                    raise SystemExit(f"record_live: not trusting {repo} (it has its own Claude Code config)")
                time.sleep(1.5)
            elif "? for shortcuts" in text or "bypass permissions on" in text.lower():
                break
            time.sleep(0.5)
        time.sleep(1)
        for ch in a.request:  # typed, so the recording shows it being asked
            shot.tmux("send-keys", "-t", shot.SESSION, "-l", ch)
            grab()
            time.sleep(0.05)
        shot.tmux("send-keys", "-t", shot.SESSION, "Enter")
        end, closed = time.time() + a.max, None
        while time.time() < end:
            time.sleep(0.5)
            grab()
            if closed is None and done():
                closed = time.time()
            if closed and time.time() - closed > 12:  # the closing turn's summary
                break
        if not closed:
            sys.exit(f"no task closed in {a.max} s; screens kept: {len(frames)} (nothing written)")
    finally:
        shot.tmux("kill-session", "-t", shot.SESSION, check=False)
    write(frames, a)


def thin(events, n):
    """T-0979: at most about n screens, one per equal slice of the recording's time (the newest in each), the last
    kept; each screen is a full redraw, so dropping one loses nothing but a moment."""
    if not n or len(events) <= n:
        return events
    end, out, slot = events[-1][0] or 1, [], None
    for e in events:
        s = int(e[0] / end * n)
        if out and slot == s:
            out[-1] = e
        else:
            out.append(e)
        slot = s
    return out


def kept(frames):
    """T-2040: Claude Code's folder-trust question is this machine's setup, not the product: never a frame of the reel
    (frames are (ts, raw) or cast events [t, "o", text]: the text is last)."""
    return [f for f in frames if "trust this folder" not in shot._SGR.sub("", f[-1]).lower()]


def write(frames, a):
    cast, t, last = os.path.join(HERE, "live.cast"), 0.0, None
    events = []
    for ts, raw in kept(frames):
        t += min(MAX_GAP, ts - last) if last is not None else 0.5
        last = ts
        events.append([round(t, 3), "o", "\x1b[H\x1b[2J" + raw.rstrip("\n").replace("\n", "\r\n")])
    events.append([round(t + 4, 3), "o", ""])  # hold the last screen
    events = thin(events, a.max_frames)
    with open(cast, "w", encoding="utf-8") as f:
        f.write(json.dumps({"version": 2, "width": COLS, "height": ROWS, "title": "Sampo in Claude Code",
                            "env": {"TERM": "xterm-256color"}}) + "\n" + "".join(json.dumps(e) + "\n" for e in events))
    print(f"{len(frames)} screens, {t:.0f} s → {cast}")
    render(events, a)


def render(events, a):
    cast = os.path.join(HERE, "live.cast")
    if not a.no_svg:
        svg = os.path.join(HERE, "live.svg")
        subprocess.run(["npx", "-y", "svg-term-cli@2.1.1", "--in", cast, "--out", svg, "--window", "--width",
                        str(COLS), "--height", str(ROWS), "--padding", "12"], check=True)
        print(f"rendered → {svg} ({os.path.getsize(svg) // 1024} KB)")
    with tempfile.TemporaryDirectory(prefix="fm-live-frames-") as d:  # the MP4: each screen for as long as it showed
        lines = []
        for i, (ev, nxt) in enumerate(zip(events, events[1:])):
            png = os.path.join(d, f"{i:05d}.png")
            shot.render(shot._OSC.sub("", ev[2][len("\x1b[H\x1b[2J"):].replace("\r\n", "\n")), png, cols=COLS,
                        rows=ROWS)  # every frame the full screen: ffmpeg sizes the video by the first
            lines += [f"file '{png}'", f"duration {max(0.04, nxt[0] - ev[0]):.3f}"]
        lines.append(f"file '{png}'")
        listing = os.path.join(d, "list.txt")
        with open(listing, "w") as f:
            f.write("\n".join(lines) + "\n")
        mp4 = os.path.join(HERE, "live.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing, "-fps_mode", "vfr",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", mp4],
                       check=True)
        print(f"video → {mp4} ({os.path.getsize(mp4) // 1024} KB)")
        # T-4008: the README's still, from the same screens (it was cut by hand once, then went stale for months): the
        # last screen with a task in focus (the session tree's "· step"), as a palette PNG like the one it replaces
        pick = next((ev for ev in reversed(events) if "· step " in ANSI.sub("", ev[2])), events[-1])
        png = os.path.join(d, "still.png")
        shot.render(shot._OSC.sub("", pick[2][len("\x1b[H\x1b[2J"):].replace("\r\n", "\n")), png, cols=COLS, rows=ROWS)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", png, "-vf",
                        "split[a][b];[a]palettegen=max_colors=256:stats_mode=single[p];[b][p]paletteuse=dither=none", STILL],
                       check=True)
        print(f"still → {STILL} ({os.path.getsize(STILL) // 1024} KB)")


if __name__ == "__main__":
    main()
