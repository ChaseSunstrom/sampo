"""T-5769: the one place Sampo reads a tmux pane and types into one. Three callers had their own copy (bus.wake, the model
switch-back in modelup, the context keeper); they share this.

  valid(pane)           a pane id the statusline recorded: %N and nothing else, so no option or target syntax gets in
  state(pane)           "idle" (an empty input box), "typed", "dialog", "busy" or "gone": what is on the screen now
  state(pane, strict)   the same, but a screen with no Claude Code marker is "foreign": a bare prompt glyph is anything
  claude_here(pane, s)  (True, "") when the pane runs the Claude Code the snapshot s names, else (False, why)
  ready(pane, s)        (True, "") when typing is safe: claude_here and state(strict) idle; else (False, why)
  plain(text)           one line of plain words: control characters and shell metacharacters gone, length bounded
  send(pane, text)      the text typed literally, then Enter; False on any failure

Typing into a pane is typing into whatever runs there: a shell would run it. Every caller (the keeper, bus.wake, modelup's
switch-back) therefore asks claude_here and state(strict) first (ready). Nothing here asks a model."""
import re
import subprocess

_PROMPT = re.compile(r"^\s*[│|]?\s*[>❯]\s*(.*?)\s*[│|]?\s*$")
_CHOICE = (re.compile(r"^\s*[❯>]?\s*1\.\s", re.M), re.compile(r"^\s*2\.\s", re.M))
# What only Claude Code's own screen shows in its last lines: the footer hints, the spinner's interrupt hint, a dialog's
# cancel hint, the exit confirmation, Sampo's statusline.
_MARKS = ("? for shortcuts", "shift+tab to cycle", "⏵⏵", "esc to interrupt", "esc to cancel", "ctrl+c again to exit",
          "sampo · ")
_COMMAND = re.compile(r"claude|node|\d+(?:\.\d+)+", re.I)  # Claude Code's process shows as claude, node or its version
_PLAIN = re.compile(r"[^\w .,:()/'+%=@-]")
DEPTH = 8  # ancestors of the claude process walked to find the pane's first process


def valid(pane):
    return isinstance(pane, str) and re.fullmatch(r"%\d+", pane) is not None


def screen(pane):
    """The pane's last twelve non-blank lines, or None when tmux can't read it."""
    if not valid(pane):
        return None
    try:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return "\n".join([ln for ln in out.stdout.splitlines() if ln.strip()][-12:])


def state(pane, strict=False):
    """"idle", "typed", "dialog", "busy" (a turn runs, or no prompt in sight), "gone" (tmux can't read it) or, with
    `strict`, "foreign" (no Claude Code marker on the screen: not a Claude Code pane, whatever its prompt looks like)."""
    text = screen(pane)
    if text is None:
        return "gone"
    low = text.lower()
    if strict and not any(m in low for m in _MARKS):
        return "foreign"
    if "esc to cancel" in low or (_CHOICE[0].search(text) and _CHOICE[1].search(text)):
        return "dialog"
    if "esc to interrupt" in low:
        return "busy"
    for ln in reversed(text.splitlines()):
        m = _PROMPT.match(ln)
        if m:
            return "typed" if m.group(1) else "idle"
    return "busy"


def idle(pane, strict=False):
    return state(pane, strict) == "idle"


def plain(text, limit=300):
    """One line of plain words: anything that is not a word character, a space or one of . , : ( ) / ' + % = @ - is
    dropped (so no control character, quote, backtick, $, ;, |, &, <, >, brace or backslash survives), spaces collapse and
    the length is bounded."""
    return re.sub(r"\s+", " ", _PLAIN.sub(" ", str(text or ""))).strip()[:limit].strip()


def claude_here(pane, snap):
    """(True, "") when `pane`'s foreground command is Claude Code (claude, node or its version) and the pane's first
    process is the claude process, or an ancestor of it, that the session's snapshot recorded (its pid and the start time
    that tells a reused pid apart); else (False, why)."""
    from sampo import bus
    if not valid(pane):
        return False, "no pane"
    pid, start = snap.get("claude_pid"), snap.get("claude_start")
    if not isinstance(pid, int) or isinstance(pid, bool) or not start:
        return False, "no claude pid in the snapshot"
    try:
        out = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}\t#{pane_pid}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False, "no pane"
    if out.returncode != 0:
        return False, "no pane"
    cmd, _, ppid = out.stdout.strip().partition("\t")
    if not _COMMAND.fullmatch(cmd.strip()):
        return False, f"the pane runs {cmd.strip() or 'nothing known'}, not claude"
    info = bus._proc(pid)
    if not info or info[1] != start:
        return False, "the snapshot's claude pid is gone or reused"
    if not ppid.strip().isdecimal() or int(ppid) not in [p for p, _ in bus._chain(pid, DEPTH)]:
        return False, "the pane's pid is not the snapshot's claude pid or its parent"
    return True, ""


def ready(pane, snap):
    """T-5794: (True, "") when typing a line into `pane` is safe: the pane runs the session's live Claude Code (claude_here)
    and its screen is Claude Code's own with an empty input box (state strict: not a shell, not typed text, not a dialog,
    not a running turn); else (False, why). Every caller that types takes this, not its own lighter look."""
    ok, why = claude_here(pane, snap if isinstance(snap, dict) else {})
    if not ok:
        return False, why
    st = state(pane, strict=True)
    return (True, "") if st == "idle" else (False, {"gone": "no pane", "foreign": "not a Claude Code screen"}.get(st, st))


def send(pane, text):
    """Type `text` literally into `pane`, then Enter. False when the pane id or the text is not plain (a newline, a
    control character or a leading dash would be read as keys or options) or tmux fails."""
    if not valid(pane) or not isinstance(text, str) or not text or text.startswith("-") \
            or re.search(r"[\x00-\x1f\x7f]", text):
        return False
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], capture_output=True, timeout=10, check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
