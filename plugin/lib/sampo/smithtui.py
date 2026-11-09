"""T-1283: Smith Code's terminal UI (`fm code tui`). It runs fm code as a child with --json and draws only from that
event stream (fmcode.EVENTS), so the harness stays the source of truth: the TUI never edits, verifies or closes.

Layers, each testable without a terminal: State (events → what happened); Canvas and the panels (a grid of styled
cells); render (state + config + time → a canvas); App (keys → commands, views, the child process). main() runs the
screen through fmcodetui's own ANSI renderer (fmcodeterm; no curses)
driver, --plain its line-by-line twin and --snapshot an SVG of the screen. Pictures over prose: a station track, check
chips, change bars and a token chart; the model's own words are one key away. Themes, layout and keys are config;
panels and commands are registries extensions add to. Stdlib only."""
import contextlib
import difflib
import functools
import html
import json
import math
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata

from sampo.smithterm import PASTE_CAP, Term, colour_depth, parse_keys, session, split_tail  # noqa: E402 (T-1300)

SCHEMA = 1  # the fm code event stream this reader knows (fmcode.SCHEMA)
FM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(__file__)))), "bin", "fm")

# Themes come from a palette of a few colours. A colour is "#rrggbb" or a 0-255 index; the driver maps hex to the
# terminal's 256 colours (8 on a small terminal), NO_COLOR drops them, and the SVG snapshot uses them as they are.
PALETTES = {
    # T-2876: the illustrated forge (design/sampo/tokens.json's dark and light roles, pinned by test_code_tui.Forge;
    # design/ isn't shipped with the plugin, so the values live here): the night the forge burns in, its gold (accent:
    # what runs), the ember (hot: the live point), and steel for finished work; paper is the daylight sheet in bronze
    "night": {"bg": "#060819", "bar": "#2A2C33", "head": "#0B1030", "ink": "#EEF0FA", "dim": "#9097BC",
              "said": "#9097BC", "accent": "#FFC24D", "ok": "#6FD8A0", "bad": "#FF85BC", "warn": "#C8DC5A",
              "edge": "#5C6390", "track": "#2A2C33", "shade": "#22253A", "barink": "#EEF0FA",
              "accent2": "#6E9BFF", "ok2": "#A8D5A2", "bad2": "#FF9440", "glow": "#FFFFFF", "panel": "#0B1030",
              "hot": "#FF6A4A", "steel": "#C8D8EC", "todo": "#5C6390"},
    "paper": {"bg": "#EEF0F6", "bar": "#D6DBEA", "head": "#E2E6F1", "ink": "#12163A", "dim": "#555B80",
              "said": "#555B80", "accent": "#7A5A26", "ok": "#1F7A45", "bad": "#B0186E", "warn": "#5F6A0A",
              "edge": "#7C819F", "track": "#C9CEDD", "shade": "#B9BFD2", "barink": "#12163A", "paint": True,
              "accent2": "#2448B8", "ok2": "#4E9F1D", "bad2": "#C2571A", "glow": "#FFFFFF", "panel": "#FFFFFF",
              "hot": "#E0461A", "steel": "#555B80", "todo": "#7C819F"},
    # the look before the forge (T-2876): kept as the classic preset, and under signal and riso as they were
    "classic": {"bg": "#0D1420", "bar": "#1B2638", "head": "#131C2B", "ink": "#E7ECF4", "dim": "#8A98AE",
              "said": "#9AA8BC", "accent": "#86A8FF", "ok": "#47C98B", "bad": "#FF6B6B", "warn": "#FFC83D",
              "edge": "#33445F", "track": "#2E3D58", "shade": "#2A3650", "barink": "#C9D3E3",
              "accent2": "#C792FF", "ok2": "#B8F26B", "bad2": "#FFA26B", "glow": "#FFFFFF", "panel": "#111B2B"},
    "classic_paper": {"bg": "#F6F7F9", "bar": "#E1E6EE", "head": "#E9EDF3", "ink": "#1B2330", "dim": "#5E6B7E",
              "said": "#4F5B6C", "accent": "#2F54C9", "ok": "#12805C", "bad": "#C42B2B", "warn": "#9A6200",
              "edge": "#C3CCD9", "track": "#CDD5E1", "shade": "#C9D1DD", "barink": "#2B3442", "paint": True,
              "accent2": "#8B3FD1", "ok2": "#4E9F1D", "bad2": "#C2571A", "glow": "#FFFFFF", "panel": "#FFFFFF"},
}
# palette = "cb" (the foreman-ui mod's word too, T-1496): pass blue and fail orange over a theme's own colours, told
# apart with red-green colour blindness; signal is night with them (T-1300)
CB = {"night": dict(ok="#4DA3FF", ok2="#9AD4FF", bad="#FF8A1F", bad2="#FFC46B", warn="#F2D544", accent="#C4A8FF",
                    accent2="#FF8FD8"),
      "paper": dict(ok="#1D5FB8", ok2="#3B7DD8", bad="#B34D00", bad2="#C2571A", accent="#7B3FC9", accent2="#B0308F")}
PALETTES["signal"] = dict(PALETTES["classic"], **CB["night"])  # T-2876: as it was before the forge
# palette = "riso" (T-1512, the pane's word too): risograph inks (green, fluorescent pink, yellow, cornflower; deeper
# cuts of them on a light theme) over a theme's own colours; the riso theme is them on an uncoated paper stock
RISO = {"night": dict(ok="#00A95C", ok2="#5EC8E5", bad="#FF48B0", bad2="#FF7477", warn="#FFE800", accent="#62A8E5",
                      accent2="#FFB511"),
        "paper": dict(ok="#00784A", ok2="#14935A", bad="#C4126C", bad2="#E2457A", warn="#835300", accent="#0062A0",
                      accent2="#B0420F")}
PALETTES["riso"] = dict(PALETTES["classic_paper"], bg="#F2EDE1", bar="#E3DAC6", head="#EAE3D2", panel="#F8F4EB", ink="#1E2235",
                        dim="#655D4E", said="#5A5242", barink="#2B2A33", edge="#C8BDA6", track="#D3C8B1",
                        shade="#D8CEBA", **RISO["paper"])
INKS = {"cb": CB, "riso": RISO}  # a palette's colours for a dark theme (night) and a light one (paper)
_BG, _INK = PALETTES["night"]["bg"], PALETTES["night"]["ink"]


def _theme(p):
    """Every style from a palette; a light palette paints the screen (a dark terminal would show through)."""
    chip = lambda c: {"fg": p["bg"], "bg": c, "bold": True}
    return {"screen": dict({"fg": p["ink"], "bg": p["bg"]}, **({"paint": True} if p.get("paint") else {})),
            "text": {}, "strong": {"fg": p["ink"], "bold": True}, "dim": {"fg": p["dim"]},
            "said": {"fg": p["said"], "italic": True}, "accent": {"fg": p["accent"], "bold": True},
            "ok": {"fg": p["ok"]}, "bad": {"fg": p["bad"], "bold": True}, "warn": {"fg": p["warn"]},
            "border": {"fg": p["edge"]}, "title": {"fg": p["accent"], "bold": True}, "track": {"fg": p["track"]},
            "chart": {"fg": p["accent"]}, "shade": {"fg": p["shade"]}, "select": {"fg": p["bg"], "bg": p["accent"]},
            "brand": chip(p["accent"]), "header": {"fg": p["ink"], "bg": p["head"]},
            "header_dim": {"fg": p["dim"], "bg": p["head"]}, "bar": {"fg": p["barink"], "bg": p["bar"]},
            "bar_dim": {"fg": p["dim"], "bg": p["bar"]}, "key": {"fg": p["warn"], "bg": p["bar"], "bold": True},
            "input": {"fg": p["ink"], "bg": p["bar"], "bold": True}, "chip_ok": chip(p["ok"]),
            "chip_bad": chip(p["bad"]), "chip_warn": chip(p["warn"]), "chip_accent": chip(p["accent"]),
            "chip_todo": {"fg": p["dim"], "bg": p["bar"]}, "accent2": {"fg": p["accent2"]}, "ok2": {"fg": p["ok2"]},
            "bad2": {"fg": p["bad2"]}, "glow": {"fg": p["glow"]}, "panel": {"bg": p["panel"]},
            # T-2876: the heat language: what runs glows hot, what is finished cools to steel (a palette without them,
            # classic, signal, riso or an ink overlay, keeps its accent and ok, so nothing it showed changes)
            "hot": {"fg": p.get("hot", p["accent"]), "bold": True}, "steel": {"fg": p.get("steel", p["ok"])},
            "chip_steel": chip(p.get("steel", p["ok"])), "todo": {"fg": p.get("todo", p["track"])}}


def _mono():
    """No colours: what had a background is reversed, the rest keeps its weight."""
    return {n: dict({k: v for k, v in spec.items() if k in ("bold", "italic")}, **({"reverse": True} if "bg" in spec
                                                                                   and n != "screen" else {}))
            for n, spec in _theme(PALETTES["night"]).items()}


PRESETS = {"night": lambda: _theme(PALETTES["night"]), "paper": lambda: _theme(PALETTES["paper"]),
           "signal": lambda: _theme(PALETTES["signal"]), "riso": lambda: _theme(PALETTES["riso"]), "mono": _mono,
           "classic": lambda: _theme(PALETTES["classic"]),  # T-2876: the look before the forge, dark and light
           "classic_paper": lambda: _theme(PALETTES["classic_paper"])}
THEME = PRESETS["night"]()
BORDERS = {"rounded": "╭╮╰╯─│", "square": "┌┐└┘─│", "heavy": "┏┓┗┛━┃", "double": "╔╗╚╝═║", "ascii": "++++-|"}
LIVE_THEMES = ("modern", "forge")
DEFAULTS = {
    "preset": "night",
    "motion": "full",
    "live_theme": "modern",
    "palette": "default",
    "theme": THEME,
    "border": "rounded",
    "layout": {"top": ["track"], "columns": [["log"], ["checks", "files", "time", "tokens"]], "widths": [58, 42],
               "heights": [[1], ["auto", "auto", "auto", 1]], "narrow": ["checks", "log"],
               "narrow_heights": ["auto", 1], "narrow_below": 80},
    "keys": {"q": "quit", "n": "new", "s": "stop", "r": "resume", "?": "help", "d": "diff", "h": "history",
             "m": "model", "t": "thoughts", "\t": "focus", "KEY_UP": "up", "KEY_DOWN": "down", "k": "up", "j": "down",
             "\n": "expand", "KEY_ENTER": "expand", "KEY_PPAGE": "page_up", "KEY_NPAGE": "page_down", "G": "follow",
             "p": "projects", ",": "settings", "z": "hush", "L": "looks", "/": "search", "g": "queue", "T": "tickets",
             "u": "undo", "P": "pr", "F": "photo", "B": "breakout", "I": "backlog"},
    "ext": {},  # T-1657: each extension's settings ({name: {key: value}}), its declared defaults (API.setting)
}
HINTS = [("n", "new"), ("p", "projects"), ("d", "diff"), ("h", "history"), ("t", "thoughts"), ("?", "keys"),
         ("q", "quit")]
CHIP = {"done": (" ✓ ", "chip_steel"), "now": (" ● ", "chip_accent"), "todo": ("   ", "chip_todo"),
        "fail": (" ✖ ", "chip_bad")}
NODE = {"done": ("●", "steel"), "now": ("◉", "hot"), "todo": ("○", "todo"), "fail": ("✖", "bad")}  # T-2876: heat
LINK = {"done": ("━", "steel"), "now": ("━", "hot"), "fail": ("━", "bad"), "todo": ("─", "todo")}
BLOCKS = "▁▂▃▄▅▆▇█"
RAIL = "▀"  # T-1512: the 3D track's built rail: its lit top in the link's colour, its side in shadow under it
PROBE = RAIL  # the glyphs whose width the app asks the terminal at start (East Asian ambiguous: 1 or 2 columns)
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TEMPO = {"running": 10, "thinking": 5, "waiting": 2}  # T-1512: the spinner's frames a second, by agent_state
MOTIONS = ("full", "reduced", "off")
LOG_HIDES = ("plan", "task", "before")  # the track and Checks show these; --plain prints them
SINGLE_BELOW = 60  # columns: a terminal narrower than this shows the activity log alone


# ---------------------------------------------------------------- state: what the events say happened

class NewerStream(ValueError):
    """A stream from a newer fm code: said on the screen, not skipped like a malformed event."""


class State:
    def __init__(self):
        self.request = self.task = self.model = self.worker = self.error = None
        self.next = self.stall = self.containment = None  # T-1762: a failure's next command, a live stall's words
        self.first = False  # T-1349: this close is the project's first
        self.plan, self.criteria, self.steps, self.step = {}, [], [], 0
        self.actions, self.log, self.verify, self.files, self.stat = [], [], [], {}, {}
        self.tokens, self.calls, self.status, self.closed = [0, 0], [], "idle", ""
        self.first_ts = self.last_ts = None
        self.heard = None  # T-1336: seconds a quiet CLI turn has printed nothing, from its last working or stalled event
        self.marks = {}  # when each step (1, 2, …) and the final checks ("checks") began
        self.ext = {}  # extensions' own data for this run

    def apply(self, e):
        """Take one event; an unknown or malformed one changes nothing (a newer fm code may send more)."""
        kind = e.get("event") if isinstance(e, dict) else None
        fn = getattr(self, f"_on_{kind}", None) if isinstance(kind, str) and kind.isidentifier() else None
        if fn:
            ts = e.get("ts")
            if "ts" in e and not (isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts)):
                e = {k: v for k, v in e.items() if k != "ts"}  # T-1306: a time that isn't a finite number isn't one
            e = {k: v if k not in ("text", "cmd", "action") else "" if v is None else str(v)
                 for k, v in e.items()}  # T-1926: the panels join, pad and strip these as words, whatever was sent
            # T-1306: a handler that trips on a bad field leaves the state as it was. One level of copies covers the
            # handlers here: each validates before it changes anything nested. ponytail: the copy grows with the log
            # (quadratic over a run); fine at hundreds of events, validate per handler if runs reach tens of thousands.
            before = {k: v.copy() if isinstance(v, (list, dict)) else v for k, v in vars(self).items()}
            try:
                fn(e)
            except Exception as x:
                vars(self).clear()
                vars(self).update(before)
                if isinstance(x, NewerStream):
                    raise
                return self
            if "ts" in e:
                self.first_ts = self.first_ts or e["ts"]
                self.last_ts = e["ts"]
            if kind not in ("stalled", "working", "tokens"):  # T-1762: anything it does after a stall ends it
                self.stall = None
        for h in (HANDLERS.get(kind, []) + HANDLERS.get("*", [])) if isinstance(kind, str) else []:
            t0, why = time.monotonic(), None
            try:
                h(self, e)
            except Exception as x:  # an extension's handler that throws is said once and dropped
                why = f"{type(x).__name__}: {x}; switched off"
            took = time.monotonic() - t0
            if why is None and took > HANDLER_BUDGET:  # T-1315: one over its budget too
                why = f"handler {getattr(h, '__qualname__', '?')} took {took * 1000:.0f} ms; switched off"
            if why:
                for hs in HANDLERS.values():
                    if h in hs:
                        hs.remove(h)
                self.note("error", text=f"extension {getattr(h, '__module__', '?')}: {why}")
        return self

    def note(self, kind, **fields):
        if self.log and self.log[-1].get("kind") == "working":  # T-1329: the ticking line goes at the next one
            self.log.pop()
        self.log.append(dict(fields, kind=kind))
        self.heard = None  # anything else said: the CLI was heard, or the turn is over

    def _on_start(self, e):
        if int(e.get("schema") or 1) > SCHEMA:
            raise NewerStream(f"sampo smith's event stream is schema {e.get('schema')}; this TUI reads {SCHEMA}: update Smith")
        if e.get("resume") and self.plan:  # fm code --resume sends no plan again: the screen keeps the run's
            self.task, self.status = e.get("task") or self.task, "running"
            return
        self.__init__()
        self.request, self.model, self.worker = (None if e.get(k) is None else str(e[k])  # the header joins them
                                                 for k in ("request", "model", "worker"))
        box = e.get("containment") if isinstance(e.get("containment"), dict) else {}
        self.containment = str(box.get("label") or "")[:40] or None  # T-1402
        self.task = e.get("task")
        self.status = "planning"

    def _on_plan(self, e):
        p = e.get("plan") if isinstance(e.get("plan"), dict) else {}
        self.plan = p
        self.criteria = [{"text": str(c.get("text", "")), "verify": str(c.get("verify", "")), "state": "todo"}
                         for c in p.get("criteria") or [] if isinstance(c, dict)]
        self.steps = [{"text": str(s), "state": "todo"} for s in p.get("steps") or []]
        self.note("plan", text=str(p.get("title", "")), sub=f"{p.get('type', '')} {p.get('tier', '')}".strip(),
                  checks=len(self.criteria), steps=len(self.steps))

    def _on_task(self, e):
        self.task, self.status = e.get("task"), "running"
        self.note("task", text=str(e.get("text") or e.get("task") or ""))

    _on_resume = _on_task

    def _on_step(self, e):
        n = int(e.get("step") or 0)
        for i, s in enumerate(self.steps, 1):
            s["state"] = "done" if i < n else "now" if i == n else s["state"]
        self.step, self.status, self.marks[n] = n, "running", e.get("ts")
        self.note("step", n=n, text=self.steps[n - 1]["text"] if 0 < n <= len(self.steps) else "")

    def _on_say(self, e):
        self.note("say", text=e.get("text", ""))

    def _on_tokens(self, e):
        self.tokens = [int(e.get("total_in") or 0), int(e.get("total_out") or 0)]
        self.calls.append(int(e.get("call_in") or 0))

    def _on_action(self, e):
        a = {k: e.get(k) for k in ("action", "target", "result", "detail")}
        a["step"] = self.step
        self.actions.append(a)
        self.note("action", **a)

    def _on_worker(self, e):
        self.note("worker", text=e.get("text", ""))

    def _on_revert(self, e):
        self.note("revert", file=e.get("file"), reason=e.get("reason"))

    def _on_fix(self, e):
        self.status = "fixing"
        self.note("fix", text=e.get("text", ""))

    def _on_verify(self, e):
        self.verify.append(e)
        n, bad, label = int(e.get("criterion") or 0), bool(e.get("exit")), e.get("label") or "verify"
        if 0 < n <= len(self.criteria):
            c = self.criteria[n - 1]
            c["state"], c["first"] = ("fail" if bad else "pass"), label == "before"
        if label == "before":  # the red run before the work: the Checks panel's; --plain prints it
            self.note("before", n=n, bad=bad, cmd=e.get("cmd") or "", detail=e.get("output"))
        else:
            for s in self.steps:
                s["state"] = "done"
            self.marks.setdefault("checks", e.get("ts"))
            self.note("verify", n=n, bad=bad, cmd=e.get("cmd") or "", detail=e.get("output"))

    def _on_changed(self, e):
        self.files[int(e.get("step") or self.step)] = [str(f) for f in e.get("files") or []]
        if isinstance(e.get("stat"), dict):
            self.stat = {str(k): v for k, v in e["stat"].items() if isinstance(v, list) and len(v) == 2
                         and all(type(n) is int for n in v)}  # the Changed title sums them

    def _on_closed(self, e):
        if isinstance(e.get("tokens"), list) and len(e["tokens"]) == 2:  # first: a bad count changes nothing
            self.tokens = [int(e["tokens"][0] or 0), int(e["tokens"][1] or 0)]
        self.status, self.closed = "closed", e.get("text", "")
        for s in self.steps:
            s["state"] = "done"
        self.note("closed", text=e.get("text", ""))
        self.note("receipt", text=self.receipt(e))  # T-1305
        if isinstance(e.get("first_in"), str) and e["first_in"]:  # T-1349: once a project, a brighter close
            self.first = True
            self.note("first", text=f"first proved change in {e['first_in'][:60]}")

    def receipt(self, e):
        """T-1305: the closed run in one line: task, checks passed, files and lines, tokens, cost, time, commit."""
        files = sorted({f for fs in self.files.values() for f in fs})
        add, rm = (sum(v[i] for v in self.stat.values()) for i in (0, 1))
        commit = re.search(r"committed ([0-9a-f]{7,40})", str(e.get("text") or ""))
        usd = e.get("usd")
        end = e.get("ts") if isinstance(e.get("ts"), (int, float)) else self.last_ts  # apply() sets last_ts after
        secs = (end - self.first_ts) if self.first_ts is not None and end is not None else None
        parts = [str(self.task or e.get("task") or "the run"),
                 f"checks {sum(c['state'] == 'pass' for c in self.criteria)}/{len(self.criteria)}",
                 f"{len(files)} file{'s' * (len(files) != 1)}" + (f" +{add} −{rm}" if self.stat else ""),
                 f"{sum(self.tokens):,} tokens"]
        parts += [f"${usd:.2f}"] if isinstance(usd, (int, float)) and not isinstance(usd, bool) else []
        parts += [duration(max(0.0, secs))] if secs is not None else []
        parts += [commit.group(1)[:7]] if commit else ["not committed"]
        return " · ".join(parts)

    def _on_note(self, e):  # T-1328: which runner the run uses and why, or a caution about it
        self.note("note", text=str(e.get("text", "")))

    def _on_tool(self, e):  # T-1329: a worker or planner CLI's tool use, as it happens
        self.note("tool", action=str(e.get("action") or ""), text=str(e.get("target") or ""))

    def _on_working(self, e):  # T-1329: a quiet CLI turn's heartbeat, one line that ticks
        self.note("working", text=str(e.get("text") or "working"))
        self.heard = _secs(e.get("silent"))

    def _on_stalled(self, e):  # T-1336: a CLI turn silent past FM_STALL, with its likely cause: a warning that stays
        self.note("stalled", text=str(e.get("text") or "stalled"))
        self.stall = str(e.get("text") or "stalled").strip()  # T-1762: its card, until the turn shows life
        self.heard = _secs(e.get("secs"))

    def _on_aside(self, e):  # T-1770: a question asked beside the run (fm code --ask): its answer; the run unchanged
        q = _printable_line(e.get("question"))
        if e.get("text"):
            self.note("aside", text=f"? {q}")
            for line in str(e["text"]).splitlines() or [""]:
                self.note("answer", text="".join(ch for ch in line if ch.isprintable() or ch == "\t"))
        else:
            self.note("aside", text=f"? {q}: no answer ({_printable_line(e.get('error')) or 'it ended without one'})")

    def _on_answer(self, e):  # T-1320: a question answered: no task, nothing changed
        self.status = "answered"
        for line in str(e.get("text", "")).splitlines() or [""]:
            self.note("answer", text=line)

    def _on_blocked(self, e):
        self.status, self.error = "blocked", e.get("reason") or e.get("text")
        self.note("blocked", text=e.get("text", ""))
        self._next(e)

    def _on_error(self, e):
        self.status, self.error = "error", e.get("text")
        self.note("error", text=e.get("text", ""))
        self._next(e)

    def _next(self, e):  # T-1334: the one command to run now, under the failure
        if e.get("next"):
            self.note("next", text=str(e["next"]))
            self.next = str(e["next"])[:200]  # T-1762: its card runs it on Enter

    def _on_exit(self, e):  # the TUI's own: the child ended
        if self.status in ("planning", "running", "fixing", "idle") and e.get("code"):
            self.status = "stopped"
            if e.get("stderr"):
                self.note("error", text=e["stderr"].strip().splitlines()[-1])


def _secs(v):
    """A count of seconds from an event, or None for anything else (a bool, a string, NaN, a negative)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 else None


def duration(s):
    s = max(0, int(s))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s // 60 % 60:02d}m"


def stations(st):
    """[(label, sub, state)] along the track: the plan, each step, the checks, the close."""
    planned = st.marks.get(1)
    nodes = [("plan", duration(planned - st.first_ts) if isinstance(planned, (int, float)) and st.first_ts else "",
              "done" if st.plan else "now" if st.status == "planning" else "todo")]
    for i, s in enumerate(st.steps, 1):
        n = sum(a["step"] == i for a in st.actions)
        start, end = st.marks.get(i), st.marks.get(i + 1) or st.marks.get("checks") or st.last_ts
        took = duration(end - start) if isinstance(start, (int, float)) and isinstance(end, (int, float)) else ""
        sub = "  ".join(x for x in (f"{n} action{'s' * (n != 1)}" if n else "", took if s["state"] != "todo" else "") if x)
        nodes.append((s["text"], sub, s["state"]))
    after = [v for v in st.verify if v.get("label") != "before"]
    passed = sum(c["state"] == "pass" for c in st.criteria)
    check = ("done" if st.criteria and passed == len(st.criteria) and after else
             "fail" if after and after[-1].get("exit") and st.status != "fixing" else
             "now" if after or st.status == "fixing" else "todo")
    nodes.append(("checks", f"{passed}/{len(st.criteria)} pass" if after else "", check))
    sha = next((w for w in st.closed.split() if len(w) >= 7 and all(c in "0123456789abcdef" for c in w)), "")
    nodes.append(("close", sha or (st.status if st.status in ("blocked", "error", "stopped") else ""),
                  "done" if st.status == "closed" else "fail" if st.status in ("blocked", "error", "stopped") else "todo"))
    return nodes


# ---------------------------------------------------------------- canvas and widgets

class Canvas:
    """A grid of (character, style) cells; .text() is what a test reads, to_svg what a reader sees."""
    def __init__(self, w, h):
        self.w, self.h = max(w, 1), max(h, 1)
        self.cells = [[(" ", "text")] * self.w for _ in range(self.h)]
        self.bg = [[None] * self.w for _ in range(self.h)]  # the colour under a cell with no background of its own

    def tint(self, y, x, h, w, colour):
        for r in range(max(0, y), min(self.h, y + h)):
            for c in range(max(0, x), min(self.w, x + w)):
                self.bg[r][c] = colour

    def _clear(self, y, x, n):
        """T-1516: before n cells from x are written over: a wide character cut at either edge loses its other half."""
        row = self.cells[y]
        if row[x][0] == "" and x:
            row[x - 1] = (" ", row[x - 1][1])
        if x + n < self.w and row[x + n][0] == "":
            row[x + n] = (" ", row[x + n][1])

    def put(self, y, x, s, style="text", right=None):
        """s at (y, x), clipped at `right` (exclusive) or the edge; the column after it."""
        if not 0 <= y < self.h:
            return x
        end, last, row = min(self.w, right if right is not None else self.w), None, self.cells[y]
        for ch in str(s):
            if _mark(ch):  # an accent joins the letter before it, in the same cell
                if last is None:
                    continue
                row[last] = (row[last][0] + ch, style)
                if x == last + 1 and _vs16(row[last][0][0], row[last][0]):  # a narrow pictograph turned two columns wide
                    if x >= end:
                        row[last] = (" ", style)  # the second column doesn't fit: a space
                        break
                    self._clear(y, x, 1)
                    row[x] = ("", style)
                    x += 1
                continue
            wide = _wide(ch)
            if x + wide >= end:
                if 0 <= x < end:
                    self._clear(y, x, 1)
                    row[x] = (" ", style)  # half a wide character doesn't fit: a space
                break
            if x >= 0:
                self._clear(y, x, 1 + wide)
                row[x], last = (ch if ch.isprintable() else " ", style), x
                if wide:  # it fills two columns: the second is an empty cell, so the row stays w columns
                    row[x + 1] = ("", style)
            x += 1 + wide
        return x

    def line(self, y, x, segments, right):
        """A line of (text, style) segments from x, clipped at right; the last one gets the ellipsis."""
        for text, style in segments:
            if x >= right:
                break
            x = self.put(y, x, fit(text, right - x, strip=False), style, right=right)
        return x

    def fill(self, y, style, x=0, right=None):
        self.put(y, x, " " * ((right if right is not None else self.w) - x), style)

    def text(self):
        return "\n".join("".join(c for c, _ in row).rstrip() for row in self.cells)

    def style_of(self, needle):
        """The style of the first cell of needle's first appearance (tests ask what a line looks like)."""
        for row in self.cells:
            i = "".join(c for c, _ in row).find(needle)
            if i >= 0:
                return row[i][1]
        return None


def _mark(ch):
    """T-1516: a character that fills no column of its own: an accent, a variation selector (U+FE0F), an enclosing
    mark, a skin-tone modifier."""
    return bool(unicodedata.combining(ch)) or unicodedata.category(ch) in ("Mn", "Me") or "\U0001f3fb" <= ch <= "\U0001f3ff"


def _vs16(base, cluster):
    """A pictograph (a heart, a check) followed by U+FE0F: kitty, WezTerm, iTerm2 and Windows Terminal draw it in two
    columns. A digit or letter keycap base stays one. ponytail: category So stands for 'has an emoji presentation';
    an emoji table if a symbol that has none turns up."""
    return "\ufe0f" in cluster and unicodedata.category(base) == "So"


def _wide(ch):
    return unicodedata.east_asian_width(ch) in "WF"


def _cells(cluster):
    """The columns a base character with its marks fills, by the rule put draws by: marks none, a wide (CJK, emoji)
    character two, a pictograph with U+FE0F two. ponytail: an emoji ZWJ sequence counts by its parts."""
    base = cluster[0]
    return 0 if _mark(base) else 2 if _wide(base) or _vs16(base, cluster) else 1


def _clusters(s):
    """s as clusters: each base character with the marks after it (lazily: a huge log line costs no more than a short one)."""
    cur = ""
    for ch in s:
        if cur and not _mark(ch):
            yield cur
            cur = ""
        cur += ch
    if cur:
        yield cur


def _rclusters(s):
    """The clusters of s from its end."""
    marks = ""
    for ch in reversed(s):
        if _mark(ch):
            marks = ch + marks
        else:
            yield ch + marks
            marks = ""
    if marks:
        yield marks


def width(s):
    """The columns s fills on a terminal."""
    return sum(map(_cells, _clusters(str(s))))


def _take(clusters, n):
    """The longest run from the front of clusters that fills at most n columns."""
    out, used = [], 0
    for c in clusters:
        used += _cells(c)
        if used > n:
            break
        out.append(c)
    return out


def pad(s, n):
    """s padded with spaces to n columns (an f-string's :<n counts characters, not columns)."""
    s = str(s)
    return s + " " * max(0, n - width(s))


def fit(s, n, strip=True):
    s = " ".join(str(s).split()) if strip else str(s).replace("\n", " ")
    got = _take(_clusters(s), n)  # stops at n columns, so a huge log line costs no more than a short one
    if sum(map(len, got)) == len(s):
        return s
    return "".join(_take(_clusters(s), n - 1)) + "…" if n > 0 else ""


def tail(s, n):
    """A path in n columns, keeping its end (the file name)."""
    if sum(map(len, _take(_rclusters(s), n))) == len(s):
        return s
    end = "".join(reversed(_take(_rclusters(s), n - 1 if n > 1 else max(n, 0))))
    end = end.lstrip("".join(c for c in end if _mark(c)))  # an accent whose letter was cut
    return "…" + end if n > 1 else end


def wrap(s, n, lines=2):
    """s in at most `lines` lines of n columns, broken at spaces; the last one ends in … when it's cut."""
    out, cur = [], ""
    for word in " ".join(str(s).split()).split(" "):
        if cur and width(cur) + 1 + width(word) > n:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    out.append(cur)
    if len(out) > lines:
        out = out[:lines - 1] + [fit(" ".join(out[lines - 1:]), n)]
    return [fit(x, n) for x in out]


def box(cv, y, x, h, w, title, cfg, focused=False, note=""):
    """A bordered panel, its title and a dim note in the top edge; its inner (y, x, h, w)."""
    tl, tr, bl, br, hz, vt = BORDERS.get(cfg.get("border"), BORDERS["rounded"])
    fx = current()
    edge = "border" if not focused else rgb(fg=mix(col(cfg, "border"), col(cfg, "accent"), 0.55 + 0.45 * fx.wave(2.4)),
                                            bold=True) if fx.moving else "accent"
    if h < 2 or w < 4:
        return y, x, 0, 0
    cv.tint(y, x, h, w, col(cfg, "panel", "bg"))
    cv.put(y, x, tl + hz * (w - 2) + tr, edge)
    for r in range(y + 1, y + h - 1):
        cv.put(r, x, vt, edge)
        cv.put(r, x + w - 1, vt, edge)
    cv.put(y + h - 1, x, bl + hz * (w - 2) + br, edge)
    if title and w > 8:
        end = cv.put(y, x + 2, f" {fit(title, min(w - 8, max(4, w - 8 - (width(note) + 2 if note else 0))))} ", "title")
        if note and end + width(note) + 3 < x + w:  # the title keeps 4 columns at least; the note goes if it doesn't fit
            cv.put(y, x + w - width(note) - 4, f" {note} ", "dim")
    return y + 1, x + 2, h - 2, w - 4


def _segments(cells):
    out = []
    for ch, style in cells:
        if out and out[-1][1] == style:
            out[-1] = (out[-1][0] + ch, style)
        else:
            out.append((ch, style))
    return out


def columns(values, w, height=3):
    """values as a column chart `height` rows tall, newest on the right; [row text, …] top first."""
    cw = max(1, min(6, (w + 1) // max(len(values), 1) - 1))
    vals = values[-max(1, (w + 1) // (cw + 1)):]
    top = max(vals, default=0) or 1
    out = []
    for r in range(height):
        base = (height - 1 - r) * 8
        cells = [BLOCKS[min(8, e - base) - 1] if e > base else " " for e in
                 (max(1, round(v * height * 8 / top)) if v else 0 for v in vals)]
        out.append(" ".join(c * cw for c in cells))
    return out


# ---------------------------------------------------------------- colour and motion (T-1300)

def rgb(fg=None, bg=None, bold=False, italic=False):
    """A computed style: colours worked out for this cell at this instant (a gradient, a pulse, a fade)."""
    return ("rgb", fg, bg, bold, italic)


def resolve(style, cfg, cell_bg=None):
    """{fg, bg, bold, italic, reverse} for a cell: a theme style by name, or a computed one; a cell with no
    background of its own sits on its panel's tint (cell_bg)."""
    if isinstance(style, tuple) and style[:1] == ("rgb",):
        _, fg, bg, bold, italic = style
        spec = {k: v for k, v in (("fg", fg), ("bg", bg), ("bold", bold), ("italic", italic)) if v not in (None, False)}
    else:
        spec = dict(((cfg or {}).get("theme") or THEME).get(style) or {})
    if "bg" not in spec and cell_bg is not None and not spec.get("reverse"):
        spec["bg"] = cell_bg
    return spec


def col(cfg, name, key="fg"):
    return (((cfg or {}).get("theme") or THEME).get(name) or {}).get(key)


def mix(a, b, k):
    """Colour a → b by k (0..1); with one of them missing (mono, NO_COLOR) the other, or None."""
    if a is None or b is None:
        return a if b is None else b
    ka, kb, k = _rgb(a), _rgb(b), max(0.0, min(1.0, k))
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * k) for x, y in zip(ka, kb))


def ease_out(k):
    k = max(0.0, min(1.0, k))
    return 1 - (1 - k) ** 3


LIVE = ("planning", "running", "fixing")


def checks_running(st):
    """The run's checks are going: from their first result to the run's end (a rule's verify event, Breakout)."""
    return "checks" in st.marks and st.status in LIVE
VIEW_FADE = 0.25  # T-1307: seconds a view takes to fade in
SCRUB_KEYS = ("KEY_LEFT", "KEY_RIGHT", "KEY_HOME", "KEY_END")  # T-1307: a replay's steps (fixed: not in tui.toml)
CIRCLED = "①②③④⑤⑥⑦⑧⑨"  # T-1350: the rail's ticket numbers


def _ticket_row(item):
    i, text, held = item
    return [(f" {CIRCLED[i] if i < len(CIRCLED) else i + 1} ", "chip_accent"), ("  " + _one_line(text), "text")] + (
        [("   ↕ moving: arrows, space puts it down", "accent")] if held else [])
SETUP_KEYS = ("l", "i", "e")  # T-1333: the first-run fixes, where tui.toml binds nothing to them


LIVE_CLI = {"claude": "claude {prompt}", "codex": "codex {prompt}", "gemini": "gemini -i {prompt}"}  # T-1341


def live_cli(worker):
    """T-1341: (name, argv template) of the CLI a person takes a task over in: the run's runner when it has an
    interactive form and is installed, else claude; None when none is installed."""
    for name in dict.fromkeys([worker or "claude", "claude"]):
        t = LIVE_CLI.get(name)
        if t and shutil.which(shlex.split(t)[0]):
            return name, t
    return None


def brief_prompt(st):
    """T-1341: the opening prompt of a live CLI that takes over a stopped run: the task, its plan, its checks, why it
    stopped, and what happens on exit."""
    plain = lambda s: " ".join("".join(ch for ch in str(s) if ch.isprintable()).split())  # noqa: E731
    lines = [f"Smith task {st.task}: {plain((st.plan or {}).get('title') or st.request or '')}"]
    lines += [f"- {plain(c.get('text'))} (check: {plain(c.get('verify'))})" for c in st.criteria]
    lines.append(f"The automatic run stopped: {plain(st.error or st.stall or 'it was stopped')}.")
    lines.append("Finish the task here. When you exit, Smith re-runs these checks and records them.")
    return "\n".join(lines)


def _runner_found():
    """T-1333: a run has something to work with: an installed runner CLI, or a model set up."""
    from sampo import smith
    from sampo import models
    try:
        return bool(any(r.get("work") and smith._installed(n, r) for n, r in smith.all_runners().items())
                    or any(models.configured()))
    except Exception:  # a broken code.toml or models.json: fm code says why when a run starts
        return False


def setup_checks(root):
    """T-1333: (what, ok, key, fix) for what a first run needs: fm on PATH, a runner, this folder as a project."""
    from sampo import core
    return [("sampo on PATH", bool(shutil.which("sampo") or shutil.which("fm")), "l", "link it"),
            ("a runner", _runner_found(), "m", "pick one"),
            ("this folder is a project", bool(root and core.find_project(root)), "i", "make it one")]


def agent_state(st):
    """T-1512: what a live run's agent is doing, for the spinner's tempo: waiting (a quiet CLI turn, its heartbeat
    the last word), thinking (planning) or running; None when no run is live."""
    if st is None or st.status not in LIVE:
        return None
    return "waiting" if st.heard is not None else "thinking" if st.status == "planning" else "running"


def title_text(st):
    """T-1310: the terminal title: the run's state and task, printable characters only (a task's text is the run's)."""
    words = f"sampo smith: {st.status} {st.task or ''}".strip() if st is not None and st.status else "sampo smith"
    return "".join(ch for ch in words if ch.isprintable())[:80]


SCORE = {"bar": "fm-code-bars", "n": "fm-code-counts", "tok": "fm-code-counts", "col": "fm-code-columns",
         "deck": "fm-code-deck"}  # T-1626: an eased key's family (its leading letters) → the score motion that times it


def score_durations(root):
    """T-1626: seconds per key family from the project's Motion Score (motion.toml at root), for the motions SCORE
    names; {} without one, or when it can't be read. 0 ms is 1 ms: there at once."""
    path = os.path.join(root, "motion.toml")
    if not os.path.isfile(path):
        return {}
    from sampo import motion as smotion
    try:
        got = {m["name"]: m["ms"] for m in smotion.check(path)["motions"] if m["ms"] is not None}
    except (OSError, ValueError):
        return {}
    return {fam: max(got[name], 1) / 1000 for fam, name in SCORE.items() if name in got}


class Motion:
    """What render needs to animate, kept across frames: when each watched value changed, and from what. A value
    already there in the first frame is no change (opening a finished run shows it as it is); one first seen later
    is new (a fresh activity line fades in, a new number rolls up from 0)."""

    def __init__(self, score=None):
        self.seen, self.frames, self.last_change = {}, 0, -1e9
        self.score = score or {}  # T-1626: seconds per key family (bar, n, tok, col, deck) from the project's score

    def since(self, key, value, t):
        """Seconds since key's value became `value` (inf when it already was in the first frame)."""
        rec = self.seen.get(("s", key))
        if rec is None or rec[0] != value:
            rec = (value, None if rec is None and self.frames <= 1 else t)
            self.seen[("s", key)] = rec
            if rec[1] is not None:
                self.last_change = max(self.last_change, t)
        return float("inf") if rec[1] is None else max(0.0, t - rec[1])

    def eased(self, key, value, t, dur=0.7):
        """value as shown at t: it rolls from what was shown before to the new value over dur seconds (or the score's
        for the key's family)."""
        if self.score:
            dur = self.score.get(re.match(r"[a-z]*", key).group(), dur)
        rec = self.seen.get(("e", key))
        if rec is None:
            rec = (value, value, None) if self.frames <= 1 else (value, 0, t)
        elif rec[0] != value:
            rec = (value, self._at(rec, t, dur), t)
        self.seen[("e", key)] = rec
        if rec[2] is not None and t - rec[2] < dur:
            self.last_change = max(self.last_change, t)
        return self._at(rec, t, dur)

    @staticmethod
    def _at(rec, t, dur):
        target, frm, t0 = rec
        return target if t0 is None else frm + (target - frm) * ease_out((t - t0) / dur)

    def busy(self, t, window=1.6):
        return t - self.last_change < window


class FX:
    """One frame's motion: its time, the setting (full: everything moves; reduced: colours change, nothing moves;
    off: still) and the theme's colours. A frame drawn without an App (a snapshot, a test) is still."""

    def __init__(self, cfg=None, app=None, t=0.0, st=None):
        self.cfg, self.t, self.tempo = cfg or DEFAULTS, t, TEMPO.get(agent_state(st), TEMPO["running"])
        mode = self.cfg.get("motion", "full") if app is not None else "off"
        self.motion = app.motion if app is not None else None
        # a theme with no colours (mono) can't flash or fade: its chips keep their reverse; things still turn
        self.moving, self.colour = mode == "full", mode in ("full", "reduced") and col(self.cfg, "accent") is not None

    def since(self, key, value):
        return self.motion.since(key, value, self.t) if self.colour else float("inf")

    def ease(self, key, value, dur=0.7):
        return self.motion.eased(key, value, self.t, dur) if self.moving else value

    def wave(self, period):
        """0..1, a slow sine: what breathes and pulses."""
        return (math.sin(self.t * 2 * math.pi / period) + 1) / 2 if self.moving else 0.5

    def c(self, name, key="fg"):
        return col(self.cfg, name, key)

    def spin(self):
        return SPIN[int(self.t * self.tempo) % len(SPIN)]


_FX = FX()


def current():
    """The frame being drawn (render sets it): panels and extensions ask it for time and colour."""
    return _FX


def faded(style, k, cfg, under):
    """style's colours brought in from the panel's tint (k 0) to themselves (k 1): a line arriving."""
    spec = resolve(style, cfg)
    fg = spec.get("fg") or col(cfg, "screen")
    return rgb(fg=mix(under, fg, k), bg=spec.get("bg") and mix(under, spec["bg"], k), bold=spec.get("bold", False),
               italic=spec.get("italic", False))


def _waiting(st, text):
    """An empty panel's words: still when idle; during a run a light passes over them, as a skeleton shimmers."""
    fx = current()
    if st.status not in LIVE or not fx.moving:
        return [[(text, "dim")]]
    dim, ink, d0 = fx.c("dim"), fx.c("screen"), (fx.t * 18) % (len(text) + 12)
    return [_segments([(ch, rgb(fg=mix(dim, ink, 0.8 * (1 - (d0 - i) / 6) if 0 <= d0 - i < 6 else 0)))
                       for i, ch in enumerate(text)])]


def _focus_line(segs, w, band, cfg):
    """segs on the band that marks the line being worked now; chips keep their own colours."""
    out = []
    for t, style in segs:
        spec = resolve(style, cfg)
        out.append((t, style if spec.get("bg") or spec.get("reverse") else rgb(
            fg=spec.get("fg") or col(cfg, "screen"), bg=band, bold=spec.get("bold", False),
            italic=spec.get("italic", False))))
    return out + [(" " * max(0, w - sum(width(t) for t, _ in segs)), rgb(bg=band))]


# ---------------------------------------------------------------- panels: (state, width, height, app) → lines
# Each panel returns lines of (text, style) segments. height is None when the layout measures it (an "auto"
# panel), or the inner rows a stretching panel has to fill.

def _track(st, w, h, app):
    """The run as a line of stations (✓ done, the one being worked, an empty slot to come, ✖ failed), each labelled
    under it; a long plan on a narrow screen gets one-cell nodes. In motion: the station being worked pulses and its
    spinner turns, a light runs along the link into it, done links are a gradient, and a close sends a wave along."""
    fx = current()
    nodes = stations(st)
    n = len(nodes)
    big = (w - 3) / (n - 1) >= 7
    marks = CHIP if big else NODE
    cw = 3 if big else 1
    xs = [round(i * (w - cw) / (n - 1)) for i in range(n)]
    rows = [[(" ", "text")] * w for _ in range(3)]
    ok, ok2, accent, glow, track = fx.c("ok"), fx.c("ok2"), fx.c("accent"), fx.c("glow"), fx.c("track")
    steel = fx.c("steel")
    rail = app is not None and app.glyphs.get(RAIL) == 1 and accent is not None  # T-1512: measured, and in colour
    for i, (_, _, state) in enumerate(nodes):
        glyph, style = marks[state]
        if state == "now" and fx.moving:
            glyph = f" {fx.spin()} " if big else fx.spin()
            if fx.colour:
                style = rgb(fg=col(fx.cfg, "chip_accent"), bg=mix(accent, glow, 0.1 + 0.4 * fx.wave(1.7)), bold=True) \
                    if big else rgb(fg=mix(accent, glow, 0.5 * fx.wave(1.7)), bold=True)
        for k, ch in enumerate(glyph):
            rows[0][xs[i] + k] = (ch, style)
        if i < n - 1:
            span = list(range(xs[i] + cw + (1 if big else 0), xs[i + 1] - (1 if big else 0)))
            into = nodes[i + 1][2]
            for j, x in enumerate(span):
                ch, style = LINK[into]
                if into == "done":  # T-2876: built track has cooled to steel (a palette without steel: its ok, as before)
                    style = rgb(fg=steel if steel != ok else mix(ok, ok2, (x / max(1, w - 1))))
                elif into == "now" and fx.moving:  # a light running toward the station being worked
                    d = (fx.t * 22) % (len(span) + 10) - j
                    style = rgb(fg=mix(mix(track, accent, 0.6), glow, (1 - d / 8) if 0 <= d < 8 else 0), bold=True)
                if rail and into != "todo":  # built: a rail standing up; what's to come stays a thin line
                    fg = resolve(style, fx.cfg).get("fg")
                    ch, style = RAIL, rgb(fg=fg, bg=mix(fg, "#000000", 0.45))
                rows[0][x] = (ch, style)
    wave = fx.since("track:status", st.status)
    first = getattr(st, "first", False)  # T-1349: a project's first close: two brighter, wider passes
    if st.status == "closed" and fx.moving and wave < (3.6 if first else 1.8):  # done: a bright wave runs the track
        p = (wave % 1.8) / 1.3 * (w + 16) - 8
        for x, (ch, style) in enumerate(rows[0]):
            k = min(1.0, max(0.0, 1 - abs(x - p) / (11 if first else 7)) * (1.15 if first else 1))
            if k and ch != " ":
                spec = resolve(style, fx.cfg)
                rows[0][x] = (ch, rgb(fg=mix(spec.get("fg"), glow, 0.9 * k), bg=spec.get("bg") and mix(
                    spec["bg"], glow, 0.5 * k), bold=True))
    gap = (w - cw) // (n - 1)
    for r in (1, 2):
        last = fit(nodes[-1][r - 1], max(1, gap - 1))
        starts = xs[:-1] + [w - width(last)]
        for i, node in enumerate(nodes):
            room = width(last) if i == n - 1 else starts[i + 1] - starts[i] - 1
            if room < 4 and node[2] not in ("now", "fail"):
                continue
            style = {"now": "accent", "done": "text", "fail": "bad"}.get(node[2], "dim") if r == 1 else "dim"
            text = node[r - 1] if r == 1 or width(node[r - 1]) <= room else node[r - 1].split("  ")[-1]  # time first
            x = starts[i]
            for ch in fit(text, max(room, 4)):  # a wide glyph fills two cells: the second is empty, as Canvas.put does
                cw_ = width(ch)
                if x + cw_ > w:
                    break
                if cw_:
                    rows[r][x] = (ch, style)
                    if cw_ == 2:
                        rows[r][x + 1] = ("", style)
                elif x:
                    rows[r][x - 1] = (rows[r][x - 1][0] + ch, style)
                x += cw_
    if app is not None and app.tickets:  # T-1350: the rail of tickets that run after this one
        rail = [("next ", "dim")]
        for i, t in enumerate(app.tickets[:len(CIRCLED)]):
            rail += [(f"{CIRCLED[i]} ", "accent"), (fit(_one_line(t), 24) + "   ", "text")]
        return [_segments(r) for r in rows] + [rail]
    return [_segments(r) for r in rows]


def _checks(st, w, h, app):
    """Each criterion as a chip and its words; a chip that just changed flashes bright and settles."""
    if not st.criteria:
        return _waiting(st, "checks come with the plan")
    fx, out = current(), []
    for i, c in enumerate(st.criteria):
        text, name = (" PASS ", "chip_ok") if c["state"] == "pass" else (" RED  ", "chip_warn") if c.get("first") \
            else (" FAIL ", "chip_bad") if c["state"] == "fail" else (" ···· ", "chip_todo")
        since = fx.since(f"check:{i}", (c["state"], bool(c.get("first"))))
        style = name if since >= 1.1 else rgb(fg=col(fx.cfg, name), bg=mix(fx.c("glow"), col(fx.cfg, name, "bg"),
                                                                        ease_out(since / 1.1)), bold=True)
        first, *more = wrap(c["text"], max(8, w - 8))
        out.append([(text, style), ("  ", "text"), (first, "text")])
        out += [[("        ", "text"), (line, "text")] for line in more]
    return out


def _files(st, w, h, app):
    """Each changed file's added and removed lines as a bar (a gradient), longest first; bars and numbers grow."""
    stat = st.stat or {f: [0, 0] for fs in st.files.values() for f in fs}
    if not stat:
        return _waiting(st, "nothing changed yet")
    fx = current()
    rows = sorted(stat.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0]))
    top = max(1, max(a + d for _, (a, d) in rows))
    nums = max(len(f" +{a} −{d}") for _, (a, d) in rows)
    nw = max(6, min(max(width(p) for p, _ in rows), w - nums - 8))
    bw = max(2, w - nw - nums - 1)
    ok, ok2, bad, bad2 = fx.c("ok"), fx.c("ok2"), fx.c("bad"), fx.c("bad2")
    out = []
    for path, (a, d) in rows:
        plus = round(fx.ease(f"bar+:{path}", max(1 if a else 0, round(a * bw / top))))
        minus = round(fx.ease(f"bar-:{path}", max(1 if d else 0, min(bw - plus, round(d * bw / top)))))
        bar = [("▆", rgb(fg=mix(ok, ok2, j / max(1, bw - 1)))) for j in range(plus)] + \
              [("▆", rgb(fg=mix(bad, bad2, j / max(1, bw - 1)))) for j in range(minus)]
        shown_a, shown_d = round(fx.ease(f"n+:{path}", a)), round(fx.ease(f"n-:{path}", d))
        heat = fx.since(f"heat:{path}", (a, d))  # a file that just changed glows, then cools to plain text
        name = "text" if heat >= 3 or not fx.colour else rgb(fg=mix(fx.c("glow"), fx.c("screen"), ease_out(heat / 3)),
                                                             bold=True)
        out.append([(pad(tail(path, nw), nw), name), (" ", "text")] + _segments(bar) + [
            (" " * max(0, bw - plus - minus), "text"), (f" +{shown_a}".ljust(len(f" +{a}")), "ok"),
            (f" −{shown_d}".ljust(len(f" −{d}")) if d else "", "bad")])
    return out


def _tokens(st, w, h, app):
    """Each model call's prompt size as a column (shaded floor to top, rising when it arrives), newest on the right,
    sat on the panel's floor; the totals under it, rolling."""
    if not st.calls:
        return _waiting(st, "no model calls yet")
    fx = current()
    tall = min(10, max(3, (h or 5) - 1))
    vals = [fx.ease(f"col:{i}", v, 0.9) for i, v in enumerate(st.calls)]
    accent, accent2, hot = fx.c("chart") or fx.c("accent"), fx.c("accent2"), fx.c("hot")
    top = hot if hot and hot != fx.c("accent") else accent2  # T-2876: the forge heats gold to ember; others as before
    lines = [[(row, rgb(fg=mix(accent, top, (tall - 1 - r) / max(1, tall - 1))))]
             for r, row in enumerate(columns(vals, w, tall))]
    roll = lambda key, v: human(round(fx.ease(key, v))).rjust(len(human(v)))  # rolls in place: labels stay put
    lines.append([(roll("tok:in", st.tokens[0]), "strong"), (" in   ", "dim"), (roll("tok:out", st.tokens[1]), "strong"),
                  (" out", "dim")])
    return [[]] * max(0, (h or 0) - len(lines)) + lines


def spans(st):
    """[(label, start, end)] of the run's phases (plan, each step, the checks) in seconds from its start."""
    if not st.first_ts:
        return []
    at = lambda v: isinstance(v, (int, float))
    marks = [("plan", st.first_ts)] + [(f"step {k}", st.marks[k]) for k in sorted(k for k in st.marks if k != "checks")
                                       if at(st.marks[k])]
    marks += [("checks", st.marks["checks"])] if at(st.marks.get("checks")) else []
    ends = [m[1] for m in marks[1:]] + [st.last_ts]
    return [(label, start - st.first_ts, end - st.first_ts) for (label, start), end in zip(marks, ends)]


def _time(st, w, h, app):
    """Where the time went: each phase as a bar on the run's own clock, done ones a gradient, the live one
    shimmering."""
    sp = spans(st)
    if not sp:
        return _waiting(st, "starts with the run")
    fx = current()
    total = max(1, sp[-1][2])
    dw = max(len(duration(e - s)) for _, s, e in sp) + 2
    bw = max(4, w - 8 - dw)
    ok, ok2, accent, glow, bad, steel = fx.c("ok"), fx.c("ok2"), fx.c("accent"), fx.c("glow"), fx.c("bad"), fx.c("steel")
    out = []
    for i, (label, s, e) in enumerate(sp):
        a = max(0, min(bw - 1, int(s * bw / total)))  # T-1922: a phase stamped before the run padded gigabytes
        b = max(a + 1, min(bw, round(e * bw / total)))
        live = i == len(sp) - 1 and st.status not in ("closed", "answered")
        cells = []
        for x in range(a, b):
            if not live:  # T-2876: a finished phase has cooled to steel (no steel in the palette: its ok, as before)
                c = steel if steel != ok else mix(ok, ok2, x / max(1, bw - 1))
            elif st.status in ("blocked", "error", "stopped"):
                c = bad
            else:
                d = (fx.t * 14) % (b - a + 8) - (x - a)
                c = mix(accent, glow, 0.7 * (1 - d / 5) if fx.moving and 0 <= d < 5 else 0)
            cells.append(("▆", rgb(fg=c)))
        out.append([(f"{label:<8}", "dim"), (" " * a, "text")] + _segments(cells) + [
            (" " * (bw - b), "text"), (duration(e - s).rjust(dw), "accent" if live else "dim")])
    return out


def entry_segments(e, w, thoughts=True):
    """An Activity line; None for one the panel leaves out (the model's own words unless thoughts are on)."""
    k = e.get("kind")
    if k == "first":  # T-1349
        return [("  ✦ ", "accent"), (e.get("text", ""), "strong")]
    if k == "receipt":  # T-1305
        return [("  ▣ ", "accent"), ("receipt  ", "dim"), (e.get("text", ""), "text")]
    if k == "step":
        head = f" {e['n']} "
        return [("──", "border"), (head, "chip_accent"), (" " + e["text"] + " ", "strong"),
                ("─" * max(0, w - len(head) - len(e["text"]) - 4), "border")]
    if k == "say":
        return [("  ┊ ", "border"), (e["text"], "said")] if thoughts else None
    if k == "action":
        if e.get("result") == "refused":
            return [("  ⊘ ", "bad"), (f"{e.get('action') or '':<7}", "dim"), (str(e.get("target") or ""), "bad"),
                    ("  ", "text"), (" GUARD ", "chip_bad")]
        bad = e.get("result") == "error"
        return [(f"  {'!' if bad else '✓'} ", "warn" if bad else "steel"), (f"{e.get('action') or '':<7}", "dim"),
                (str(e.get("target") or ""), "warn" if bad else "text")]
    if k == "revert":
        return [("  ↺ ", "warn"), (f"{'revert':<7}", "dim"), (str(e.get("file")), "warn"), ("  ", "text"),
                (" PUT BACK ", "chip_warn")]
    if k == "verify":
        return [("  ", "text"), (" FAIL " if e["bad"] else " PASS ", "chip_bad" if e["bad"] else "chip_ok"),
                (f"  check {e['n']}", "dim")]
    if k == "before":
        return [("  ", "text"), (" RED " if e["bad"] else " GREEN ", "chip_warn" if e["bad"] else "chip_ok"),
                (f"  check {e['n']} before the work", "dim")]
    if k == "plan":
        return [(" PLAN ", "chip_accent"), (f"  {e['text']}", "strong"),
                (f"  {e['sub']}, {e['checks']} checks, {e['steps']} steps", "dim")]
    if k == "task":
        return [("  ", "text"), (e["text"], "dim")]
    if k == "fix":
        return [("  ↻ ", "warn"), (e["text"], "warn")]
    if k == "worker":
        return [("  ⚙ ", "accent"), (e["text"].strip(), "text")]
    if k == "note":
        return [("  i ", "accent"), (e["text"], "text")]
    if k == "tool":
        return [("  · ", "dim"), (f"{e.get('action', '')[:7]:<7}", "dim"), (e["text"], "text")]
    if k == "working":
        return [("  … ", "accent"), (e["text"].strip(), "dim")]
    if k == "stalled":
        return [("  ⚠ ", "warn"), (e["text"].strip(), "warn")]
    if k == "closed":
        return [(" DONE ", "chip_steel"), ("  " + e["text"], "steel")]
    if k == "answer":
        return [("  ", "text"), (e.get("text", ""), "text")]
    if k == "aside":  # T-1770
        return [("  ", "text"), (e.get("text", ""), "accent")]
    if k in ("blocked", "error"):
        return [(" STOP ", "chip_bad"), ("  " + e["text"], "bad")]
    if k == "next":
        return [("  Next: ", "dim"), (e["text"], "accent")]
    return [("  ", "text"), (e.get("text", ""), "dim")]


def _printable_line(v, n=300):
    """T-1770: one line of text from another process, printable only."""
    return "".join(ch for ch in str(v or "").replace("\n", " ") if ch.isprintable())[:n]


def entry_text(e):
    """An Activity line as plain text (--plain): a refusal says why, and no control character reaches the terminal."""
    text = "".join(t for t, _ in entry_segments(e, 0) or []).strip(" ─")
    if e.get("kind") == "action" and e.get("result") == "refused" and e.get("detail"):
        text += f" — {e['detail']}"
    return "".join(c if c.isprintable() else " " for c in text)


def _log(st, w, h, app):
    """The run as it happens; a line that just arrived fades in from the panel."""
    out, thoughts, fx = [], app is None or app.thoughts, current()
    under, now = fx.c("panel", "bg"), None
    for i, e in enumerate(st.log):
        if e.get("kind") == "answer":  # T-1320: an answer is read whole, wrapped to the panel
            out += [[("  ", "text"), (part, "text")] for part in wrap(e.get("text", ""), max(10, w - 2), lines=200)]
            continue
        segs = None if e.get("kind") in LOG_HIDES else entry_segments(e, w, thoughts)
        if segs is None:
            continue
        if app is not None and app.search and app.search.lower() not in "".join(t for t, _ in segs).lower():
            continue  # T-1313: / keeps only the lines that match
        arrived = fx.since(f"log:{i}", e.get("kind"))
        if arrived < 0.6:
            segs = [(t, faded(style, ease_out(arrived / 0.6), fx.cfg, under)) for t, style in segs]
        if arrived < 1.6 and e.get("result") == "refused":  # the guard's no lands with a flash
            hot = rgb(fg=col(fx.cfg, "chip_bad"), bg=mix(fx.c("glow"), col(fx.cfg, "chip_bad", "bg"),
                                                         ease_out(arrived / 1.6)), bold=True)
            segs = [(t, hot if t == " GUARD " else style) for t, style in segs]
        if app is not None and app.selected == i:
            segs = [(pad("".join(t for t, _ in segs), w), "select")]
        else:
            now = len(out)
        out.append(segs)
        if app is not None and i in app.expanded and e.get("detail"):
            out += [[("      ", "text"), (part, "dim")] for line in str(e["detail"]).splitlines()[:30]
                    for part in wrap(line, max(10, w - 6), lines=4)]
    band = mix(under, fx.c("accent"), 0.16) if under and fx.c("accent") else None
    if st.status in LIVE and now == len(out) - 1 and band:  # the line being worked now sits on a band
        out[now] = _focus_line(out[now], w, band, fx.cfg)
    if not out and st.status in LIVE:  # started, nothing done yet: say what it's doing, mid-panel
        words = {"planning": "planning the work", "fixing": "fixing the checks"}.get(st.status, "working")
        line = [(" " * max(0, (w - len(words) - 2) // 2), "text"), ((fx.spin() if fx.moving else "◆") + " ", "accent")] + _waiting(st, words)[0]
        return [[]] * max(0, ((h or 1) - 1) // 2) + [line]
    return out or _welcome(w, h, fx, app)


def _welcome(w, h, fx, app=None):
    """The screen before any run: Smith's mark, breathing, and the keys that start something; before a project's
    first run (T-1333), what a run needs, each missing one with the key that fixes it, and the first question."""
    a, b = fx.c("accent"), fx.c("accent2")
    keys = [("n", "new request"), ("h", "past runs"), ("m", "model"), ("?", "keys")]
    chips = [seg for k, word in keys for seg in ((f" {k} ", "chip_accent"), (f" {word}   ", "dim"))]
    used = sum(width(t) for t, _ in chips)
    left = lambda n: [(" " * max(0, (w - n) // 2), "text")]
    lines = [left(1) + [("◆", rgb(fg=mix(a, b, fx.wave(3.2)), bold=True) if a else "accent")], [],
             left(12) + [("Smith Code", "strong")],
             left(42) + [("plan, edit, check and close, each guarded", "dim")], []]
    checks = app.first_run() if app is not None else []
    if checks:
        rows = [[(" ✓ " if ok else " ✗ ", "chip_ok" if ok else "chip_bad"), (f" {what}", "text")]
                + ([("   ", "text"), (f" {key} ", "chip_accent"), (f" {fix}", "dim")] if not ok else [])
                for what, ok, key, fix in checks]
        rows += [[], [(" e ", "chip_accent"), (" ask: explain this repo", "text")]]
        wide = max(sum(width(t) for t, _ in r) for r in rows)
        lines += [left(wide) + r if r else [] for r in rows] + [[]]
    lines.append(left(used) + chips)
    top = max(0, ((h or len(lines)) - len(lines)) // 2)
    return [[]] * top + lines


def _plan(st, w, h, app):  # the plan in words: not in the default layout, one config line away
    if not st.plan:
        return [[("no plan yet", "dim")]]
    out = [[(line, "strong")] for line in wrap(st.plan.get("title", ""), w)]
    for c in st.criteria:
        out += [[("· ", "dim"), (c["text"], "text")], [("  $ ", "dim"), (c["verify"], "dim")]]
    return out


def _verify(st, w, h, app):  # each check's command and its output: also one config line away
    out = []
    for e in st.verify[-8:]:
        bad = bool(e.get("exit"))
        out.append([(" FAIL " if bad else " PASS ", "chip_bad" if bad else "chip_ok"), ("  " + (e.get("cmd") or ""), "text")])
        out += [[("   ", "text"), (x, "dim")] for x in str(e.get("output") or "").strip().splitlines()[-4:]]
    return out or [[("not run yet", "dim")]]


def _title(st, name):
    if name == "track":
        return (st.plan.get("title") or st.request or "No run yet"), (
            f"{st.plan.get('type', '')} {st.plan.get('tier', '')}".strip() if st.plan else "")
    if name == "log":
        return "Activity", (f"{len(st.actions)} actions" if st.actions else "")
    if name == "checks" and st.criteria:
        return "Checks", f"{sum(c['state'] == 'pass' for c in st.criteria)}/{len(st.criteria)}"
    if name == "files" and st.stat:
        return "Changed", f"+{sum(a for a, _ in st.stat.values())} −{sum(d for _, d in st.stat.values())}"
    if name == "tokens" and st.calls:
        return "Tokens per call", f"{len(st.calls)} calls"
    if name == "time" and st.first_ts:
        return "Time", duration((st.last_ts or st.first_ts) - st.first_ts)
    return PANELS[name][0], ""


PANELS = {"track": ("Run", _track), "log": ("Activity", _log), "checks": ("Checks", _checks),
          "files": ("Changed", _files), "time": ("Time", _time), "tokens": ("Tokens", _tokens),
          "plan": ("Plan", _plan), "verify": ("Check output", _verify)}
BUILTIN_PANELS = {name: fn for name, (_, fn) in PANELS.items()}  # T-1315: never switched off (an extension's are)


# ---------------------------------------------------------------- render

def human(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


STATUS_CHIP = {"running": "chip_accent", "planning": "chip_warn", "fixing": "chip_warn", "closed": "chip_steel",
               "answered": "chip_steel",
               "blocked": "chip_bad", "error": "chip_bad", "stopped": "chip_bad"}


def elapsed(st, app):
    if not st.first_ts:
        return ""
    s = int((time.time() if app is not None and app.running() else st.last_ts or st.first_ts) - st.first_ts)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _header(cv, st, app):
    cv.fill(0, "header")
    fx, brand = current(), " ⊖ SAMPO CODE "  # T-2876: the anvil-stone mark
    a, b, glow, ink = fx.c("accent"), fx.c("accent2"), fx.c("glow"), col(fx.cfg, "brand")
    sweep = (fx.t % 7) / 1.4 * (len(brand) + 8) - 4 if fx.moving else -99  # a glint crosses the name now and then
    for i, ch in enumerate(brand):
        bg = mix(mix(a, b, i / (len(brand) - 1)), glow, 0.55 * max(0.0, 1 - abs(i - sweep) / 3))
        cv.put(0, i, ch, rgb(fg=ink, bg=bg, bold=True) if bg else "brand")
    x = len(brand)
    clock = elapsed(st, app)
    right = "   ".join(v for v in (st.worker and f"worker {st.worker}" or st.model or "",
                                   getattr(st, "containment", None) or "", clock and f"⏱ {clock}") if v)  # T-1402
    right = f"{right}  " if right else ""
    if st.request:
        cv.line(0, x + 2, [(st.request, "header")], cv.w - len(right) - 2)
    cv.put(0, cv.w - len(right), right, "header_dim")


def _status(cv, st, app):
    y = cv.h - 1
    cv.fill(y, "bar")
    fx, name = current(), STATUS_CHIP.get(st.status, "chip_todo")
    live = st.status in LIVE
    since = fx.since("status:chip", st.status)
    chip = rgb(fg=col(fx.cfg, name), bg=mix(fx.c("glow"), col(fx.cfg, name, "bg"), ease_out(since / 1.2)), bold=True) \
        if since < 1.2 else name
    segs = [(f" {fx.spin()} {st.status.upper()} " if live and fx.moving else f" {st.status.upper()} ", chip)]
    if st.task:
        segs += [("  ", "bar"), (st.task, "bar")]
    if st.steps:
        segs += [("   ", "bar"), (f"step {st.step}/{len(st.steps)}", "bar_dim")]
    if live and st.heard is not None:  # T-1336; T-1310: past 10 s, say what the run waits on
        segs += [("   ", "bar"), (("model: " if st.heard >= 10 else "")
                                  + f"last heard {duration(st.heard)} ago", "bar_dim")]
    if app is not None and app.rule is not None:  # T-1657: the layout a [[rule]] switched to
        segs += [("   ", "bar"), (f" rule {app.rule} ", "chip_accent")]
    if app is not None and app.message:
        segs += [("   ", "bar"), (app.message, "key")]
    hints = [seg for k, word in HINTS for seg in ((f" {k} ", "key"), (f"{word} ", "bar_dim"))]
    hint_w = sum(width(t) for t, _ in hints) + 1
    room = cv.w - hint_w if cv.w - hint_w > 36 else cv.w
    cv.line(y, 0, segs, room)
    if room < cv.w:
        cv.line(y, room, hints, cv.w)


def _draw(name, st, w, h, app):
    fn = PANELS[name][1]
    ext = app is not None and BUILTIN_PANELS.get(name) is not fn
    if ext and name in app.panels_off:
        return [[(app.panels_off[name], "bad")]]
    t0 = time.monotonic()
    try:
        return [[(str(t), s if isinstance(s, tuple) else str(s)) for t, s in line] for line in fn(st, w, h, app)]
    except Exception as e:  # an extension's panel that fails, or returns something else than lines, says why
        return [[(f"panel {name} failed: {type(e).__name__}: {e}", "bad")]]
    finally:  # T-1315: an extension's panel over its budget PANEL_STRIKES frames running is switched off
        took = time.monotonic() - t0
        if ext:  # counted once a frame: a panel can be drawn twice in one (measured, then drawn)
            frame, strikes = app.panel_strikes.get(name, (None, 0))
            if frame != app.motion.frames:
                strikes = strikes + 1 if took > PANEL_BUDGET else 0
            app.panel_strikes[name] = (app.motion.frames, strikes)
            if strikes >= PANEL_STRIKES:
                app.panels_off[name] = f"panel {name} took {took * 1000:.0f} ms; switched off"
                app.state.note("error", text=app.panels_off[name])


def _panel(cv, st, app, cfg, name, y, x, h, w, lines):
    title, note = _title(st, name)
    if name == "log" and app is not None and app.search:  # T-1313: the filter and what it kept
        note = f"{len(lines)} matching {fit(app.search, 24)}"
    focused = app is not None and app.focused() == name
    iy, ix, ih, iw = box(cv, y, x, h, w, title, cfg, focused, note)
    hint = PANEL_HINTS.get(name) if focused else None
    if hint and w > width(hint) + 8:  # T-1313: the focused panel's own keys, in its bottom edge
        cv.put(y + h - 1, x + 2, f" {hint} ", "dim")
    follow = name == "log" and (app is None or app.follow)
    top = max(0, len(lines) - ih) if follow else min(app.scroll if app else 0, max(0, len(lines) - ih))
    for r, segs in enumerate(lines[top:top + ih]):
        cv.line(iy + r, ix, segs, ix + iw)


def render(st, w, h, cfg, app=None, t=None):
    """The whole screen for this state at time t (the App's clock): header, panels per the layout, the status bar,
    any open view. Motion is a function of state and time, so any instant can be drawn (tests, snapshots, video)."""
    global _FX
    if app is not None:
        app.motion.frames += 1
        t = app.clock() if t is None else t
    _FX = FX(cfg, app, t or 0.0, st)
    cv = Canvas(w, h)
    prompt = app is not None and app.mode == "prompt"
    search = app is not None and app.mode == "search"  # T-1313
    caption = app.caption if app is not None and not (prompt or search) else None  # T-1309: the tour's words
    y0, body_h = 1, h - 2 - (2 if prompt else 1 if search or caption else 0)  # T-1346: the request box, its preview
    lay = cfg.get("layout") or DEFAULTS["layout"]
    if app is None or app.mode != "layout":  # T-1657: a rule's layout while its events hold; the editor edits the file's
        lay = rule_layout(cfg, st, app)
    elif app is not None:
        app.rule = None
    narrow, single = w < int(lay.get("narrow_below", 80)), w < SINGLE_BELOW
    order = []
    for name in [n for n in lay.get("top") or [] if n in PANELS and not single]:  # full-width rows, as tall as they need
        lines = _draw(name, st, w - 4, None, app)
        ph = min(len(lines) + 2, max(3, body_h // 3))
        _panel(cv, st, app, cfg, name, y0, 0, ph, w, lines)
        y0, body_h = y0 + ph, body_h - ph
    columns_ = [["log"]] if single else [lay.get("narrow") or ["log"]] if narrow else lay.get("columns") or [["log"]]
    extra = [n for n in WIDE_COLUMN if all(n not in col for col in columns_) and n not in (lay.get("top") or [])]
    if not narrow and not single and w >= WIDE_ABOVE and extra and len(columns_) > 1:  # T-1347: room for a third
        columns_ = [*columns_, extra]
    widths = [100] if len(columns_) == 1 else list(lay.get("widths") or [])
    widths = (widths + [100 // len(columns_)] * len(columns_))[:len(columns_)]
    heights = [lay.get("narrow_heights") or []] if narrow else list(lay.get("heights") or [])
    x = 0
    for ci, names_ in enumerate(columns_):
        cw = w - x if ci == len(columns_) - 1 else max(12, w * widths[ci] // max(sum(widths), 1))
        names = [n for n in names_ if n in PANELS] or ["log"]
        weights = ((list(heights[ci]) if ci < len(heights) else []) + [1] * len(names))[:len(names)]
        measured = {n: _draw(n, st, max(1, cw - 4), None, app) for n, wt in zip(names, weights) if wt == "auto"}
        fixed = {n: min(len(lines) + 2, body_h * 45 // 100) for n, lines in measured.items()}
        rest = body_h - sum(fixed.values())
        flex = sum(wt for wt in weights if wt != "auto") or 1
        y = y0
        for pi, name in enumerate(names):
            share = fixed[name] if name in fixed else max(3, rest * weights[pi] // flex)
            ph = y0 + body_h - y if pi == len(names) - 1 else share
            lines = measured.get(name) or _draw(name, st, max(1, cw - 4), max(1, ph - 2), app)
            _panel(cv, st, app, cfg, name, y, x, ph, cw, lines)
            order.append(name)
            y += ph
        x += cw
    if app is not None:
        app.order = order
    _header(cv, st, app)
    _status(cv, st, app)
    if app is not None:  # T-1762: a failure's or a stall's card, above the status bar (and the request box)
        _card(cv, st, app, cfg, h - 1 - (2 if prompt else 1 if search else 0))
    if prompt:  # T-1346: the cursor where the line editor has it, and where Enter sends the request
        shown = lambda s: s.replace("\n", " ↵ ")
        cv.fill(h - 3, "input")
        cv.line(h - 3, 0, [(" ask › " if app.asking else " next › " if app.ticketing else " request › ", "key"),
                           (shown(app.input[:app.line.cur]),
                                                                                 "input"), ("▏", "key"),
                           (shown(app.input[app.line.cur:]), "input")], w)
        cv.line(h - 2, 0, [("   ", "text"), (app.preview(), "dim")], w)
    if caption:
        cv.fill(h - 2, "select")
        cv.line(h - 2, 0, [(f" ▶ {caption}", "select")], w)
    if search:
        cv.fill(h - 2, "input")
        cv.line(h - 2, 0, [(" search › ", "key"), (app.search_draft, "input"), ("▏", "key"),
                           ("   Enter keeps it, Esc clears", "dim")], w)
    if app is not None and app.mode in VIEWS:
        VIEWS[app.mode](cv, st, app, cfg)
    if app is not None:  # T-1307: a switch between the run, its diff and past runs fades in instead of jumping
        if app.mode == "project" and app.grow and app.grow[0] and _FX.moving:  # T-1345: out of the card it was
            (y, x, gh, gw), k = app.grow[0], (_FX.t - app.grow[1]) / GROW
            if 0 <= k < 1:
                e = ease_out(k)
                y0, x0 = round(y * (1 - e)), round(x * (1 - e))
                y1, x1 = round(y + gh + (cv.h - y - gh) * e), round(x + gw + (cv.w - x - gw) * e)
                cv.cells = [[cell if y0 <= r < y1 and x0 <= c < x1 else (" ", "shade") for c, cell in enumerate(row)]
                            for r, row in enumerate(cv.cells)]
        k = _FX.since("view", app.mode if app.mode in ("diff", "history") else "main")
        if k < VIEW_FADE:
            e, screen = ease_out(k / VIEW_FADE), col(cfg, "screen", "bg")
            cv.cells = [[(ch, faded(s, e, cfg, cv.bg[y][x] or screen)) for x, (ch, s) in enumerate(row)]
                        for y, row in enumerate(cv.cells)]
    return cv


# ---------------------------------------------------------------- views over the main screen

KEY_NAMES = {"\t": "Tab", "\n": "Enter", "KEY_ENTER": "Enter", "KEY_UP": "↑", "KEY_DOWN": "↓", "KEY_PPAGE": "PgUp",
             "KEY_NPAGE": "PgDn"}
COMMAND_LABELS = [("new", "type a request"), ("stop", "stop the run"), ("resume", "resume it"),
                  ("diff", "the run's changes"), ("history", "past runs"), ("projects", "every Sampo project"),
                  ("model", "model or worker"), ("settings", "settings: look, layout, keys"),
                  ("looks", "your taste: 12 pairs of looks"),
                  ("backlog", "the deferred pile: revive, keep or drop each"),
                  ("hush", "motion: full, reduced, off"),
                  ("queue", "work the queue: each task in turn"),
                  ("tickets", "tickets: requests that run after this one"),
                  ("undo", "undo the closed run: a git revert of its commit"),
                  ("pr", "the closed run's pull request text; Enter posts it"),
                  ("photo", "photo booth: this frame as SVG and PNG"),
                  ("breakout", "Breakout, while the checks run"),
                  ("thoughts", "the model's own words"), ("focus", "next panel"), ("up", "up"), ("down", "down"),
                  ("expand", "open a line"), ("follow", "follow the latest"), ("search", "search the Activity"),
                  ("help", "keys"), ("quit", "quit")]


def _overlay(cv, cfg, title, note, w=None, h=None):
    """A view's box over the screen; the screen behind it fades so the view stands out."""
    cv.cells = [[(c, "shade") for c, _ in row] for row in cv.cells]
    w, h = min(w or cv.w - 8, cv.w - 2), min(h or cv.h - 4, cv.h - 2)
    y, x = max(1, (cv.h - h) // 2), max(0, (cv.w - w) // 2)
    for r in range(y, y + h):
        cv.put(r, x, " " * w)
    return box(cv, y, x, h, w, title, cfg, focused=True, note=note)


CLI = {"new": 'sampo smith "…"', "resume": "sampo smith --resume", "model": "sampo smith --model M", "settings": "sampo settings",
       "hush": "sampo settings set motion off", "diff": "git diff"}  # T-1348: the command line behind a key, where one is
CONFIRM_S = 2.0  # T-1348: q or s during a live run waits this long for the second press
WIDE_ABOVE, WIDE_COLUMN = 160, ["plan", "verify"]  # T-1347: a third column's panels, from this many columns
TIP_SKIP = {"stop", "up", "down", "focus", "expand", "follow", "help", "quit"}  # too plain to need a tip
POSTER = (("run", ("new", "stop", "resume", "model")),  # T-1512: the keys poster's groups; extensions' go under more
          ("see", ("diff", "history", "projects", "thoughts", "settings", "hush", "looks", "backlog")),
          ("move", ("focus", "up", "down", "expand", "follow", "search")), ("app", ("help", "quit")))


def _help_view(cv, st, app, cfg):
    """T-1512: the keys as a poster: keycaps grouped by what they do, each group's name in the gutter; two columns of
    groups with a line between them when the screen is wide, one column as tall as the old list when it isn't."""
    keys = {}
    for k, cmd in (cfg.get("keys") or {}).items():
        name = KEY_NAMES.get(k) or key_name(k)
        if name not in keys.setdefault(cmd, []):
            keys[cmd].append(name)
    labels = dict([*COMMAND_LABELS, *app.extra_labels])
    placed = {c for _, cmds in POSTER for c in cmds}
    blocks = []
    for name, cmds in [*POSTER, ("more", [c for c in labels if c not in placed])]:
        shown = [c for c in cmds if keys.get(c) and c in labels]
        blocks.append([line for i, c in enumerate(shown) for line in [
            [(f" {'' if i else name.upper():<4}", "title"), (" " * max(0, 8 - sum(width(n) + 3 for n in keys[c])), "text")]
            + [seg for n in keys[c] for seg in ((f" {n} ", "key"), (" ", "text"))] + [(" " + labels[c], "text")]]
            + ([[(" " * 14, "text"), (CLI[c], "dim")]] if c in CLI else [])])  # T-1348: the command line behind it
    two = cv.w >= 90
    gap = lambda bs: [line for b in bs if b for line in b + [[]]][:-1]
    cols = [gap(blocks[:2]), gap(blocks[2:])] if two else [[line for b in blocks for line in b]]
    # T-3989: 94 wide, so a column holds `sampo settings set motion off` under its key (88 cut it to "…o…")
    iy, ix, ih, iw = _overlay(cv, cfg, "Keys", "any key closes", 94 if two else 46, max(map(len, cols)) + 4)
    for c, lines in enumerate(cols):
        x = ix + c * (iw // 2)
        for i, segs in enumerate(lines[:ih - 1]):
            cv.line(iy + 1 + i, x, segs, x + (iw // 2 if two else iw))


def _diff_style(line):
    return ("strong" if line.startswith(("diff --git", "+++", "---")) else "accent" if line.startswith("@@")
            else "ok" if line.startswith("+") else "bad" if line.startswith("-") else "text")


_WORDS = re.compile(r"\w+|\s+|[^\w\s]")


def intraline(lines):
    """T-1312: {line index: [(start, end) in the text after its +/-]} for the words a changed line changed: a run of
    removed lines paired, in order, with the added run right after it (lines over 400 characters aren't compared)."""
    marks, i = {}, 0
    side = lambda s, ch: s.startswith(ch) and not s.startswith(ch * 3)
    while i < len(lines):
        j = i
        while j < len(lines) and side(lines[j], "-"):
            j += 1
        k = j
        while k < len(lines) and side(lines[k], "+"):
            k += 1
        for a, b in zip(range(i, j), range(j, k)):
            old, new = _WORDS.findall(lines[a][1:401]), _WORDS.findall(lines[b][1:401])
            pos = lambda toks: [sum(map(len, toks[:n])) for n in range(len(toks) + 1)]
            po, pn = pos(old), pos(new)
            for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
                if tag != "equal":
                    if a2 > a1:
                        marks.setdefault(a, []).append((po[a1], po[a2]))
                    if b2 > b1:
                        marks.setdefault(b, []).append((pn[b1], pn[b2]))
        i = max(k, i + 1)
    return marks


DIFF_RATE = 40  # T-1312: lines a second the diff view reveals on open


def card(st, app):
    """T-1762: the card a failure or a live stall shows over the run, or None: (kind, title, rows, keys). A failure's
    rows: what happened, what Smith did, the next command (Enter runs it); a stall's: w waits, k stops the run,
    m stops it and picks another runner to resume with. Esc hides it until the next one."""
    if app is None or app.mode != "main":
        return None
    if st.status in ("error", "blocked") and st.error:
        did = (f"blocked {st.task}: its log says why" if st.status == "blocked" else
               f"stopped; {st.task} stays as it was" if st.task else "stopped before any task: nothing ran")
        kind, rows = "failure", [("what happened", str(st.error)), ("Smith did", did),
                                 ("next", st.next or "nothing to run")]
        cli = live_cli(st.worker) if st.task else None  # T-1341
        keys = ("Enter runs it · " if st.next else "") + (f"x hands it to {cli[0]} · " if cli else "") + "Esc hides"
        title = "Stopped"
    elif st.stall and st.status in LIVE:
        kind, title = "stall", "Stalled"
        cli = live_cli(st.worker) if st.task else None  # T-1341
        rows = [("what happened", st.stall), ("Smith did", "kept the turn going; nothing is lost"),
                ("next", "w wait · k stop the run · m stop and resume with another runner"
                 + (f" · x take it over in {cli[0]}" if cli else ""))]
        keys = "w · k · m · " + ("x · " if cli else "") + "Esc hides"
    else:
        return None
    if app.card_hidden == (kind, rows[0][1]):
        return None
    return kind, title, rows, keys


def _card(cv, st, app, cfg, bottom):
    got = card(st, app)
    if not got:
        return
    _, title, rows, keys = got
    h, w = len(rows) + 2, min(cv.w, 110)
    iy, ix, ih, iw = box(cv, bottom - h, 0, h, w, title, cfg, focused=True, note=keys)
    for r, (label, text) in enumerate(rows[:ih]):
        cv.line(iy + r, ix, [(f"{label:<15}", "dim"), (_one_line(text), "bad" if r == 0 else
                                                                     "accent" if label == "next" else "text")],
                ix + iw)


def _one_line(text):
    return " ".join(str(text).split())


def _diff_view(cv, st, app, cfg):
    iy, ix, ih, iw = _overlay(cv, cfg, app.view_title or "Changes", "q closes")
    lines = app.view_lines or ["no changes"]
    app.view_scroll = max(0, min(app.view_scroll, len(lines) - ih))
    fx = current()
    shown = len(lines) if not fx.moving else int((app.clock() - app.view_opened) * DIFF_RATE) + 1
    marks = app.view_marks
    for r, line in enumerate(lines[app.view_scroll:app.view_scroll + min(ih, max(0, shown - app.view_scroll))]):
        n, style, text = app.view_scroll + r, _diff_style(line), line.expandtabs(4)
        segs, at = [(text[:1], style)], 1
        for a, b in marks.get(n, []) if "\t" not in line else []:  # a tab moves columns: marks would land wrong
            segs += [(text[at:a + 1], style), (text[a + 1:b + 1], "chip_ok" if line[0] == "+" else "chip_bad")]
            at = b + 1
        cv.line(iy + r, ix, segs + [(text[at:], style)], ix + iw)


def _list_view(title, note, row, head=None, foot=None, empty="nothing here yet", sized=False):
    """A list over the screen, the selected row highlighted and kept in sight; row(item) draws one (row(item, iw)
    when sized). head(app, iw) and foot(app, iw) are lines fixed above and below it; a view with a foot keeps its full
    height as the list changes (a search narrowing it doesn't make the box jump). On a short terminal the list keeps
    a row: the foot's lines go first (its last, the line being typed, stays longest), then the head."""
    def view(cv, st, app, cfg):
        iy, ix, ih, iw = _overlay(cv, cfg, title(app) if callable(title) else title,
                                  note(app) if callable(note) else note, 110,
                                  cv.h - 4 if foot else min(len(app.items) + 4, cv.h - 4))
        if ih < 2:
            return
        top, bottom = head(app, iw) if head else [], foot(app, iw) if foot else []
        while ih - 1 - len(top) - len(bottom) < 1:
            if len(bottom) > 1:
                bottom = bottom[1:]
            elif top:
                top = []
            else:
                bottom = []
        for r, segs in enumerate(top):
            cv.line(iy + 1 + r, ix, segs, ix + iw)
        y, lh = iy + 1 + len(top), ih - 1 - len(top) - len(bottom)
        if not app.items:
            cv.line(y, ix, [(empty, "dim")], ix + iw)
        first = max(0, app.item_index - lh + 1)
        for r, item in enumerate(app.items[first:first + lh]):
            segs = row(item, iw) if sized else row(item)
            if first + r == app.item_index:
                segs = [(pad("".join(t for t, _ in segs), iw), "select")]
            cv.line(y + r, ix, segs, ix + iw)
        for r, segs in enumerate(bottom):
            cv.line(iy + ih - len(bottom) + r, ix, segs, ix + iw)
    return view


def _backlog_row(item):
    """T-1856: one deferred item and why it was deferred."""
    from sampo import core
    return [(f" {item['id']} ", "chip_todo"), ("  " + _one_line(core.plain(item["title"])), "text")] + (
        [("  — " + _one_line(core.plain(item["why"])), "dim")] if item["why"] else [])


def _history_row(item):
    chip = {"closed": (" DONE ", "chip_ok"), "blocked": (" STOP ", "chip_bad"), "error": (" STOP ", "chip_bad"),
            "undone": (" UNDONE ", "chip_warn")}.get(  # T-1352
        item["status"], (" ···· ", "chip_todo"))
    when = time.strftime("%b %d %H:%M", time.localtime(item["ts"])) if item.get("ts") else ""
    again = [(f"  sampo smith --resume {item['task']}", "accent")] if item["status"] == "stopped" else []  # T-1338
    return [chip, ("  " + pad(item["task"] or "—", 8), "strong"), ("  " + pad(when, 12), "dim")] + again + [
        ("  " + item["request"], "text")]


def _picker_row(item):
    kind, name = item
    return [(" MODEL  " if kind == "model" else " WORKER ", "chip_accent" if kind == "model" else "chip_warn"),
            ("  " + name, "text")]


def _num(v):
    return v if isinstance(v, (int, float)) and v == v else 0


def _noticed():
    from sampo import smith
    return smith.noticed()


HOME_NOTE = "Enter opens · n new request there · , settings · Esc run screen · q quits"
CARD_W, CARD_H, GROW = 38, 6, 0.25  # T-1345: a project's card on the home, and how long Enter's grow takes
DECK_STAGE = {"planning": 0, "ready": 0, "executing": 1, "verifying": 2, "auditing": 2, "closing": 3}


def _deck_view(cv, st, app, cfg):
    """T-1345: the home as a deck: a card per project (what Smith noticed first), as many columns as fit. The focus
    ring glides from the last card to the selected one (fx.ease: at once with motion reduced or off), which is drawn
    focused once the ring lands. Each card's place is kept for the keys (deck_cols) and Enter's grow (deck_rects)."""
    note = f"/{app.jump} · Esc stops" if app.jump is not None else HOME_NOTE
    iy, ix, ih, iw = _overlay(cv, cfg, "Smith projects", note, cv.w - 2, cv.h - 2)
    if app.home_note and ih > 2:  # T-1344: what changed since the last open
        cv.line(iy, ix, [(_printable(app.home_note, iw), "dim")], ix + iw)
        iy, ih = iy + 1, ih - 1
    app.deck_rects = {}
    if ih < 2 or iw < 8:
        return
    cols = max(1, min(len(app.items) or 1, (iw + 1) // (CARD_W + 1)))
    cw, ch = (iw - (cols - 1)) // cols, CARD_H
    app.deck_cols = cols
    if not app.items:
        cv.line(iy, ix, [("no Sampo projects yet: sampo init in a repository makes one", "dim")], ix + iw)
        return
    rows = max(1, ih // ch)
    top = max(0, app.item_index // cols - rows + 1)
    for i in range(top * cols, min(len(app.items), (top + rows) * cols)):
        r, c = divmod(i, cols)
        app.deck_rects[i] = (iy + (r - top) * ch, ix + c * (cw + 1), ch, cw)
        _project_card(cv, app.items[i], *app.deck_rects[i], cfg, focused=False)
    target = app.deck_rects.get(app.item_index)
    if target:
        fx = current()
        ring = (round(fx.ease("deck_y", target[0], 0.18)), round(fx.ease("deck_x", target[1], 0.18)))
        app.deck_ring = ring
        if ring == target[:2]:
            _project_card(cv, app.items[app.item_index], *target, cfg, focused=True)
        else:
            _ring(cv, *ring, ch, cw)


def _ring(cv, y, x, h, w):
    """T-1345: the focus ring on its way between two cards: a border alone, over what's there."""
    tl, tr, bl, br, hz, vt = BORDERS["rounded"]
    cv.put(y, x, tl + hz * (w - 2) + tr, "accent")
    for r in range(y + 1, y + h - 1):
        cv.put(r, x, vt, "accent")
        cv.put(r, x + w - 1, vt, "accent")
    cv.put(y + h - 1, x, bl + hz * (w - 2) + br, "accent")


def _printable(s, n):
    return "".join(ch for ch in str(s) if ch.isprintable())[:n]


def _project_card(cv, p, y, x, h, w, cfg, focused):
    """T-1345: one project's card: its name, its runner, the active task's stations, the task, the badges."""
    noticed = p.get("kind") == "noticed"
    runner = _printable(p.get("runner") or "", 20) if isinstance(p.get("runner"), str) else ""
    note = _printable(p.get("task") or "", 12) if noticed else (f"◆ {runner}" if runner else "")
    iy, ix, ih, iw = box(cv, y, x, h, w, "Smith noticed" if noticed else str(p.get("project") or "?"), cfg,
                         focused=focused, note=note)
    for r, segs in enumerate(_card_lines(p)[:ih]):
        cv.line(iy + r, ix, segs, ix + iw)


def _card_lines(p):
    if p.get("kind") == "noticed":  # T-1339: a round Foreman queued on itself, with its newest causes
        return [[(" NOTICED ", "chip_warn"), ("  " + str(p.get("title") or ""), "dim")]] + [
            [(str(c), "text")] for c in [str(x) for x in p.get("causes") or []][-2:]]
    if p.get("error"):
        return [[(" ERROR ", "chip_bad")], [(str(p["error"]), "bad")]]
    if p.get("exists") is False:
        return [[(" GONE ", "chip_bad")], [(f"folder missing: {p.get('root')}", "dim")]]
    a = p.get("active") if isinstance(p.get("active"), dict) else None
    task = [(f"{a.get('id')} ", "strong"), (str(a.get("title") or ""), "text")] if a else [("nothing active", "dim")]
    badges = []
    for k, style in (("blocked", "chip_bad"), ("inbox", "chip_todo"), ("queue", "chip_accent")):
        n = _num(p.get(k))
        badges += [(f" {k} {n:g} ", style if n else "dim"), (" ", "text")]
    return [_card_track(a), task, badges]


def _card_track(a):
    """T-1345: the active task's stations (plan, work, checks, close) as a mini track, the one it's at lit."""
    if not a:
        return [("○──○──○──○", "todo")]
    at, names = DECK_STAGE.get(a.get("stage"), 1), ("plan", "work", "checks", "close")
    segs = []
    for i in range(4):
        segs.append(("●", "steel") if i < at else ("◉", "hot") if i == at else ("○", "todo"))  # T-2876: heat
        if i < 3:
            segs.append(("━━", "steel") if i < at else ("──", "todo"))
    done, total = a.get("steps_done"), a.get("steps_total")
    steps = f" {done}/{total}" if at == 1 and isinstance(total, int) and total else ""
    return segs + [(f"  {names[at]}{steps}", "dim")]


_SECTION = {"active": (" ACTIVE  ", "chip_accent"), "queue": (" QUEUED  ", "chip_todo"),
            "inbox": (" INBOX   ", "chip_todo"), "blocked": (" BLOCKED ", "chip_bad")}


def _work_row(it):
    """T-1330: one line of a project's view: a task by section, a past run, or why its state can't be read."""
    if it.get("kind") == "error":
        return [(" ERROR ", "chip_bad"), ("  " + str(it.get("text")), "bad")]
    if it.get("kind") == "run":
        return _history_row(it)
    if it.get("kind") == "genesis":  # T-2118: the tower's next action, first in its project's view
        g = it.get("genesis") or {}
        from sampo import core
        return [(" GENESIS ", "chip_accent" if g.get("kind") in ("ok", "done") else "chip_bad"),
                ("  " + core.genesis_line(g), "text")]
    t = it.get("task") or {}
    why = f"  ({t['reason']})" if t.get("reason") else ""
    return [_SECTION.get(it.get("section"), (" TASK    ", "chip_todo")), ("  " + pad(t.get("id") or "?", 7), "strong"),
            (f"  {str(t.get('type') or '')[:4]:<4} {str(t.get('tier') or '')}", "dim"),
            ("  " + str(t.get("title") or "") + why, "text")]


def fm_projects():
    """Every Sampo project on this machine, as fm projects --json lists them."""
    from sampo import core
    rows = [p for p in (_fm_json("projects").get("projects") or []) if isinstance(p, dict)]
    for p in rows:  # T-1344: a project whose last run ended blocked or in error ranks as failed
        q = core.project_by_slug(str(p.get("project")))
        last = history(os.path.join(q.dir, "code"))[:1] if q else []
        p["failed"] = bool(last) and last[0]["status"] in ("blocked", "error")
    return rows


def need_rank(p):
    """T-1344: a project's place on the home, smallest first: asks or blocked tasks, a failed last run, a run going, a
    queue (the longest-waiting first: oldest activity), then the rest; a missing folder last, newest first within."""
    if p.get("exists") is False:
        return (9, -_num(p.get("updated")))
    tier = 0 if p.get("waits") or p.get("blocked") else 1 if p.get("failed") else 2 if p.get("active") else \
        3 if p.get("queue") else 4
    return (tier, _num(p.get("updated")) if tier == 3 else -_num(p.get("updated")))


def _changed_line(items, seen):
    """T-1344: the one line of what moved since the home was last opened (at `seen`, None the first time)."""
    if seen is None:
        return "first look: nothing to compare with"
    names = [str(p.get("project")) for p in items if _num(p.get("updated")) > seen]
    return ("changed since you last looked: " + ", ".join(names[:4]) + (f" +{len(names) - 4} more" if len(names) > 4 else "")
            ) if names else "nothing changed since you last looked"


def fm_project_state(p):
    """A project's work, as fm state --json gives it, and its fm code runs' folder."""
    st = _fm_json("state", "-p", str(p.get("project")))
    from sampo import core
    q = core.project_by_slug(str(p.get("project")))
    st["code_dir"] = os.path.join(q.dir, "code") if q else None
    return st


def _fm_json(*args):
    r = subprocess.run([sys.executable, FM, *args, "--json"], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(" ".join((r.stderr or r.stdout).split())[-300:] or f"fm {args[0]} exit {r.returncode}")
    return json.loads(r.stdout)


# ---------------------------------------------------------------- the settings screen (T-1572)

_GROUPS = {"look": "Look", "colors": "Colours", "layout": "Layout", "keys": "Keys", "pane": "Claude Code pane"}
_WHERE = {"app": "this app · tui.toml · applies now", "plugin": "the foreman-ui pane · Claude Code's settings.json · "
                                                                "applies within seconds"}
_SET_HINTS = (("/", "search"), ("m", "changed only"), (":", "set"), ("Enter", "edit"), ("[ ]", "turn"),
              ("b", "bind a key"), ("l", "layout"), ("r", "runners"), ("Esc", "close"))
# T-1511: keys b never binds: the ones the app keeps for itself, and what each does; a key tui.toml moved one of KEPT
# to is kept too, and the last key for one of LAST isn't bound away
RESERVED = {"q": "quit", "\x1b": "back", "^C": "quit", "?": "help", "z": "hush"}
KEPT, LAST = ("quit", "help", "hush", "settings"), ("up", "down", "expand", "focus")
# in a list the arrows, PgUp/PgDn and Enter always move and open, whatever tui.toml binds them to (none locked out)
LIST_KEYS = {"KEY_UP": "up", "KEY_DOWN": "down", "KEY_PPAGE": "page_up", "KEY_NPAGE": "page_down", "\n": "expand",
             "\r": "expand", "KEY_ENTER": "expand"}
PREVIEW_KEYS, PREVIEW_SECONDS = ("theme", "palette", "motion"), 10  # T-1573: shown at once, written only when kept


def preset_theme(preset, palette="default"):
    """The styles a theme gives before any [colors] change: a palette's inks over its colours (mono has none)."""
    base = PALETTES.get(preset)
    if palette in INKS and base:  # T-2876: an ink is the look it was, its ground and all: over the night and paper before the forge
        base = PALETTES[{"night": "classic", "paper": "classic_paper"}.get(preset, preset)]
        return _theme(dict(base, **INKS[palette]["paper" if base.get("paint") else "night"]))
    return PRESETS.get(preset, PRESETS["night"])()


def _shown(v, surface):
    """A setting's value as the screen and the :set line write it: a plain word bare, anything else as its file
    has it."""
    if v is None:
        return "—"
    if isinstance(v, str) and v and v == v.strip() and v.isprintable():
        return v
    return _toml(v) if surface == "app" else json.dumps(v, ensure_ascii=False)


def _style_text(spec):
    """A [colors] style in words: "#E7ECF4 on #0D1420 bold" (the :set line still writes it as TOML)."""
    words = [str(spec["fg"])] if spec.get("fg") is not None else []
    words += [f"on {spec['bg']}"] if spec.get("bg") is not None else []
    return " ".join(words + [w for w in ("bold", "italic", "reverse", "paint") if spec.get(w)]) or "plain"


def _set_cols(iw):
    """Column widths for a settings row iw wide: (key, value, default, badge); 2-column gaps and a 2-column mark."""
    badge, kw = (14 if iw >= 80 else 6), min(24, max(8, iw // 4))
    rest = max(2, iw - 10 - kw - badge)
    return kw, rest * 55 // 100, rest - rest * 55 // 100, badge


def _badge(r, bw):
    """When a change applies: now is the quiet common case; the pane, which reads its options again within seconds
    (it polls settings.json), is the one that stands out."""
    if r["surface"] == "app":
        return " NOW ", "chip_todo"
    return (" IN SECONDS " if bw >= 14 else " SOON "), "chip_warn"


def _setting_row(r, iw):
    if "head" in r:
        h = r["head"]  # T-1657: an extension's own group is named for it
        name = _GROUPS.get(h) or (f"{h[4:]} extension" if h.startswith("ext.") else h)
        return [(f" {name.upper()} ", "chip_accent" if r["surface"] == "app" else
                 "chip_warn"), (f"  {r['count']} · {_WHERE[r['surface']]}", "dim")]
    kw, vw, dw, bw = _set_cols(iw)
    changed = r["value"] != r["default"]
    mark = ("◆ ", "accent2") if r.get("profile") is not None else ("● " if changed else "  ", "warn")  # T-1656
    segs = [mark, (pad(fit(r["key"], kw), kw) + "  ", "strong" if changed else "text")]
    style = r["group"] == "colors"
    show = lambda v: _style_text(v) if style and isinstance(v, dict) else _shown(v, r["surface"])
    spec = r["value"] if style and isinstance(r["value"], dict) else {}
    swatch = spec.get("fg", spec.get("bg"))
    if swatch is not None and vw > 8:  # a colour shows itself beside its value
        segs += [("██", rgb(fg=swatch)), (" ", "text")]
        vw -= 3
    label, chip = _badge(r, bw)
    return segs + [(pad(fit(show(r["value"]), vw), vw) + "  ", "accent" if changed else "text"),
                   (pad(fit(show(r["default"]), dw), dw) + "  ", "dim"), (" " * (bw - width(label)), "text"),
                   (label, chip)]


def _settings_head(app, iw):
    kw, vw, dw, bw = _set_cols(iw)
    when = "APPLIES" if bw >= 14 else "WHEN"
    return [[("  " + pad("SETTING", kw + 2) + pad("VALUE", vw + 2) + pad("DEFAULT", dw + 2)
              + " " * (bw - width(when)) + when, "dim")]]


def _settings_foot(app, iw):
    """A rule; the selected setting's help and choices; what the last :set did (or a setting the app skipped); the
    search or :set line, else the screen's keys."""
    from sampo import settings
    r = app.items[app.item_index] if 0 <= app.item_index < len(app.items) else None
    mine = [(f"◆ set by profile {_escaped(r['profile'])} here · ", "accent2")] if r and r.get("profile") is not None \
        else []  # T-1656: a :set writes the file's own value, which this terminal doesn't show
    if r and r.get("scope"):  # T-1611: what the app runs with comes from a narrower scope
        mine += [({"pin": "◆ pinned by the repo's policy · ", "project": "◆ this project sets it · ",
                   "run": "◆ this run sets it · "}[r["scope"]], "accent2")]
    about = [] if not r or "head" in r else mine + [(r["help"], "dim")] + (
        [("   " + " · ".join(map(str, r["choices"])), "accent2")] if r.get("choices") else [])
    first = app.set_problems[0][1] if app.set_problems else ""
    note, style = app.set_note or ((f"! {first}{settings.skip_note(first)}", "warn") if first else ("", "dim"))
    p = app.set_pending
    if p:  # T-1573: a look shown, not written: the countdown to going back
        left = max(0, math.ceil(p["until"] - app.clock()))
        note, style = f"{p['key']} = {_shown(p['value'], 'app')}: a preview, not written · reverts in {left} s", "warn"
        last = [(" any key ", "key"), (" keeps it", "dim")]
    elif app.set_typing == "bind":  # T-1511: the next key is the one bound
        last = [(f" press the key for {r['value'] if r else '?'} ", "key"), ("  Esc cancels", "dim")]
    elif app.set_typing:
        text = tail(app.set_query if app.set_typing == "search" else app.set_line, max(1, iw - 4))
        last = [(" /" if app.set_typing == "search" else " :", "key"), (text, "input"), ("▏", "key"),
                (" " * max(0, iw - 3 - width(text)), "input")]
    else:  # hints too wide for one line take the note's spare second line (a long note keeps it: they clip)
        rows = [[]]
        for k, word in _SET_HINTS:
            pair = [(f" {k} ", "key"), (f" {word}  ", "dim")]
            if rows[-1] and sum(width(s) for s, _ in rows[-1] + pair) > iw and len(rows) < 2:
                rows.append([])
            rows[-1] += pair
        notes = (wrap(note, iw) + [""])[:2]
        if len(rows) > 1 and not notes[1]:
            return [[("─" * iw, "border")], about, [(notes[0], style)]] + rows
        last = sum(rows, [])
    return [[("─" * iw, "border")], about] + [[(x, style)] for x in (wrap(note, iw) + [""])[:2]] + [last]


def _settings_note(app):
    n, on = sum("head" not in r for r in app.items), (app.cfg.get("profile") or {}).get("name")
    return " · ".join([f"{n} of {len(app.set_rows)}", f"s: saves to {SCOPE_WORDS[app.set_scope]}"]
                      + ([f"profile {_escaped(on)}"] if on is not None else [])
                      + (["changed only"] if app.set_modified else [])
                      + ([f"/{app.set_query}"] if app.set_query else []))


def _layout_view(cv, st, app, cfg):
    """T-1511: the columns side by side as the screen has them, the picked panel highlighted; the screen behind (in
    shade) already draws each move."""
    cols = cfg["layout"]["columns"]
    at, below = app._lay_pos(), max(int(cfg["layout"].get("narrow_below", 80)), SINGLE_BELOW)
    narrow = cv.w < below  # the screen shows layout.narrow (or the log alone): a move here shows only when wider
    iy, ix, ih, iw = _overlay(cv, cfg, "Layout", "Esc back", max(48, 20 * len(cols) + 4),
                              max(map(len, cols)) + 7 + 3 * narrow)
    say = f"below {below} columns the screen draws layout.narrow; a move shows once it is wider"
    for n, line in enumerate(wrap(say, max(1, iw))[:2] if narrow else []):
        cv.line(iy + ih - 4 + n, ix, [(line, "warn")], ix + iw)
    cw = max(1, iw // len(cols))
    for n, names in enumerate(cols):
        x = ix + n * cw
        cv.line(iy + 1, x, [(f" COLUMN {n + 1}", "title")], x + cw)
        for r, name in enumerate(names[:max(0, ih - 4)]):
            cv.line(iy + 3 + r, x, [(pad(" " + name, cw - 1), "select" if (n, r) == at else "text")], x + cw)
    note, style = app.set_note or ("←↑↓→ pick a panel · H J K L move it", "dim")
    cv.line(iy + ih - 1, ix, [(note, style)], ix + iw)


def _runner_row(r):
    """T-1511: a runner of the registry or code.toml, and its last test's result; T-1335: else its last ping (p) as a
    lamp: ok green, version only plain, anything else red."""
    got, ping = r.get("result"), r.get("ping")
    if got or r.get("testing") or not (ping or r.get("pinging")):
        chip = ((" ✓ ", "chip_ok") if got["ok"] else (" ✖ ", "chip_bad")) if got else (" … ", "chip_accent") if \
            r.get("testing") else ("   ", "chip_todo")
        what = got["said"] if got else "testing…" if r.get("testing") else "Enter tests it · p pings it" if \
            r["work"] else "no command: define [runners.NAME] work in code.toml"
        bad = bool(got and not got["ok"])
    elif r.get("pinging"):
        chip, what, bad = (" … ", "chip_accent"), "pinging (one tiny question)…", False
    else:
        state = str(ping.get("state") or "?")
        chip = (" ● ", "chip_ok") if state == "ok" else (" ● ", "chip_todo") if state == "version only" else \
            (" ● ", "chip_bad")
        ms = ping.get("ms")
        what = state + (f" · {ms / 1000:.1f} s" if isinstance(ms, (int, float)) else "") + (
            f" · {ping['version']}" if ping.get("version") else "")
        bad = state not in ("ok", "version only")
    return [chip, ("  " + pad(r["name"], 14), "strong"), (pad("installed" if r["installed"] else "—", 11), "dim"),
            (what, "bad" if bad else "text")]


def _runners_foot(app, iw):
    note, style = app.set_note or ("", "dim")
    return [[("─" * iw, "border")]] + [[(x, style)] for x in (wrap(note, iw) + [""])[:2]]


def _breakout_view(cv, st, app, cfg):
    """T-1556: Breakout over the screen while the checks run, stepped to the app's clock."""
    g = app.game
    g.step(min(0.5, max(0.0, _FX.t - app.game_t)))
    app.game_t = _FX.t
    end = " · you cleared it" if not g.bricks else " · out of balls" if g.over else ""
    iy, ix, ih, iw = _overlay(cv, cfg, "Breakout", f"←/→ move · Esc closes · {g.score} · {'♥' * g.lives}{end}",
                              g.w + 26, g.h + 4)  # wide enough for the title and its keys
    for r, line in enumerate(g.lines()[:ih]):
        cv.put(iy + r, ix + max(0, (iw - g.w) // 2), line[:iw], "text")


VIEWS = {"breakout": _breakout_view, "help": _help_view, "diff": _diff_view, "history": _list_view("Past runs", "Enter opens one", _history_row),
         "picker": _list_view("Run the next request with", "Enter picks", _picker_row),
         "home": lambda cv, st, app, cfg: _deck_view(cv, st, app, cfg),  # T-1345: cards, not rows
         "tickets": _list_view("Tickets", "space picks up / puts down · ↑↓ move · Delete drops · Esc back", _ticket_row,
                               empty="no tickets: during a run, n then Tab writes one"),  # T-1350
         "project": _list_view(lambda app: str((app.project or {}).get("project") or "project"),
                               "Enter/r runs a task · Enter replays a run · n new · m runner · , settings · "
                               "Esc projects",
                               _work_row),
         "settings": _list_view("Settings", _settings_note, _setting_row, _settings_head, _settings_foot,
                                "no setting matches", sized=True),
         "layout": _layout_view,
         "runners": _list_view("Runners", "Enter tests one (--version) · p pings one (a tiny question) · Esc back",
                               _runner_row,
                               foot=_runners_foot),
         "backlog": _list_view("Backlog: the deferred pile", "r revive · space keep · d drop · u undo · ↑↓ move · Esc back",
                               _backlog_row, empty="nothing is deferred"),  # T-1856
         "looks": _list_view(lambda app: f"{len(app.look_answers) + 1}/{len(app.look_pairs)} Which is more like you?",
                             "1 · 2 · n neither · Backspace back · Esc stops, nothing kept",
                             lambda item: [(item[0], "text")])}


# ---------------------------------------------------------------- colours and the SVG snapshot

def _rgb(c):
    """A theme colour as (r, g, b): "#rrggbb", or a 256-colour index."""
    if isinstance(c, str) and len(c) == 7 and c.startswith("#"):
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    i = int(c)
    if i < 16:
        base = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188),
                (17, 168, 205), (229, 229, 229)]
        r, g, b = base[i % 8]
        return (min(255, r + 50), min(255, g + 50), min(255, b + 50)) if i >= 8 else (r, g, b)
    if i < 232:
        lv = [0, 95, 135, 175, 215, 255]
        return lv[(i - 16) // 36], lv[(i - 16) % 36 // 6], lv[(i - 16) % 6]
    return (8 + (i - 232) * 10,) * 3


@functools.lru_cache(maxsize=4096)  # the terminal asks for every cell of every frame
def to_256(c):
    """The nearest 256-colour index (the 6x6x6 cube or the grey ramp) for a theme colour."""
    if isinstance(c, int):
        return c
    r, g, b = _rgb(c)
    lv = [0, 95, 135, 175, 215, 255]
    near = lambda v: min(range(6), key=lambda i: abs(lv[i] - v))
    cube = 16 + 36 * near(r) + 6 * near(g) + near(b)
    grey = 232 + min(23, max(0, round((sum((r, g, b)) / 3 - 8) / 10)))
    dist = lambda idx: sum((a - b_) ** 2 for a, b_ in zip(_rgb(idx), (r, g, b)))
    return min((cube, grey), key=dist)


def _hex(c):
    return "#%02x%02x%02x" % _rgb(c)


def to_svg(cv, cfg, title="sampo smith tui"):
    """The canvas as an SVG terminal window: each run of one style is one text element, fitted to its cells."""
    theme = cfg.get("theme") or THEME
    screen = theme.get("screen", {})
    bg, fg = _hex(screen.get("bg", _BG)), _hex(screen.get("fg", _INK))
    cw, ch, pad, chrome = 8.4, 18, 16, 34
    W, H = round(cv.w * cw + 2 * pad), round(cv.h * ch + 2 * pad + chrome)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           '<style>text{font-family:"JetBrains Mono","Cascadia Code","Noto Sans Mono","DejaVu Sans Mono",monospace;'
           'font-size:14px;white-space:pre}</style>',
           f'<rect width="{W}" height="{H}" rx="12" fill="{bg}"/>',
           f'<rect width="{W}" height="{chrome}" rx="12" fill="#0A101A"/><rect y="{chrome - 12}" width="{W}" height="12" fill="#0A101A"/>']
    for i, dot in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
        out.append(f'<circle cx="{22 + i * 20}" cy="{chrome / 2}" r="6" fill="{dot}"/>')
    out.append(f'<text x="{W / 2}" y="{chrome / 2 + 5}" text-anchor="middle" fill="#8A98AE">{html.escape(title)}</text>')
    back, head = [], len(out)  # every background goes under every glyph, so a later run's background can't cut a hairline into a bar
    for y, row in enumerate(cv.cells):
        x, under = 0, cv.bg[y]
        while x < len(row):
            style, end = row[x][1], x
            while end < len(row) and row[end][1] == style and under[end] == under[x]:
                end += 1
            spec = resolve(style, cfg, under[x])
            s_fg, s_bg = spec.get("fg"), spec.get("bg")
            if spec.get("reverse"):
                s_fg, s_bg = s_bg or screen.get("bg", _BG), s_fg or screen.get("fg", _INK)
            px, py = pad + x * cw, chrome + pad + y * ch
            if s_bg is not None:
                back.append(f'<rect x="{px:.1f}" y="{py:.1f}" width="{(end - x) * cw + 0.6:.1f}" height="{ch + 0.6:.1f}" '
                            f'fill="{_hex(s_bg)}"/>')  # +0.6: neighbouring runs overlap, no seam between them
            text = "".join(c for c, _ in row[x:end])
            k = 0
            while k < len(text):  # block glyphs as exact rects (one per run), as a terminal fills its cells
                c, j = text[k], k
                while j < len(text) and text[j] == c:
                    j += 1
                if c in BLOCKS:
                    part = (BLOCKS.index(c) + 1) / 8
                    out.append(f'<rect x="{px + k * cw:.1f}" y="{py + ch * (1 - part):.1f}" width="{(j - k) * cw + 0.6:.1f}" '
                               f'height="{ch * part + 0.6:.1f}" fill="{_hex(s_fg) if s_fg is not None else fg}"/>')
                k = j
            text = "".join(" " if c in BLOCKS else c for c in text)
            if text.strip():
                attrs = (' font-weight="bold"' if spec.get("bold") else "") + (
                    ' font-style="italic"' if spec.get("italic") else "")
                out.append(f'<text x="{px:.1f}" y="{py + ch * 0.74:.1f}" fill="{_hex(s_fg) if s_fg is not None else fg}"'
                           f' textLength="{(end - x) * cw:.1f}" lengthAdjust="spacingAndGlyphs"{attrs}>'
                           f'{html.escape(text)}</text>')
            x = end
    out[head:head] = back
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------- the app: keys, commands, views, the child

class LineEditor:
    """T-1346: the request box: text, a cursor and this launch's requests (Up/Down), with the keys a shell's line
    editor has (arrows, Home/End, Ctrl-A/E/W/U/K, Delete)."""

    def __init__(self, history=()):
        self.text, self.cur, self.history, self.at, self.draft = "", 0, list(history), None, ""

    def set(self, text):
        self.text, self.cur, self.at = text, len(text), None

    def insert(self, s):
        self.text, self.cur = self.text[:self.cur] + s + self.text[self.cur:], self.cur + len(s)

    def remember(self, text):
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)

    def key(self, k):
        """Whether k was an editing key (and done)."""
        t, i = self.text, self.cur
        if k in ("KEY_LEFT", "\x02"):
            self.cur = max(0, i - 1)
        elif k in ("KEY_RIGHT", "\x06"):
            self.cur = min(len(t), i + 1)
        elif k in ("KEY_HOME", "\x01"):
            self.cur = 0
        elif k in ("KEY_END", "\x05"):
            self.cur = len(t)
        elif k in ("KEY_BACKSPACE", "\x7f", "\b"):
            if i:
                self.text, self.cur = t[:i - 1] + t[i:], i - 1
        elif k in ("KEY_DC", "\x04"):
            self.text = t[:i] + t[i + 1:]
        elif k == "\x15":  # Ctrl-U: what's before the cursor
            self.text, self.cur = t[i:], 0
        elif k == "\x0b":  # Ctrl-K: what's after it
            self.text = t[:i]
        elif k == "\x17":  # Ctrl-W: the word before it
            j = t.rfind(" ", 0, len(t[:i].rstrip())) + 1
            self.text, self.cur = t[:j] + t[i:], j
        elif k in ("KEY_UP", "KEY_DOWN") and self.history:
            if self.at is None:
                if k == "KEY_DOWN":
                    return True
                self.draft, self.at = t, len(self.history)
            self.at = max(0, self.at - 1) if k == "KEY_UP" else self.at + 1
            if self.at >= len(self.history):
                self.text, self.at = self.draft, None
            else:
                self.text = self.history[self.at]
            self.cur = len(self.text)
        elif len(k) == 1 and k.isprintable():
            self.insert(k)
        else:
            return False
        return True


PANEL_HINTS = {"log": "/ search  ↵ open  t thoughts", "files": "d the diff", "track": "j k scroll",
               "checks": "j k scroll"}  # T-1313: the focused panel's own keys, in its bottom edge


class App:
    def __init__(self, cfg, spawn=None, root=None, opts=None, diff=None, history=None, choices=None, projects=None,
                 project_state=None, used=None, place=None, noticed=None, asker=None, queued=None):
        self.line, self.search, self.search_draft, self.files = LineEditor(), "", "", None  # T-1346, T-1313
        self.cfg, self.state = cfg, State()
        self.mode, self.input, self.message, self.done = "main", "", "", False
        self.used_path, self.used = used, set()  # T-1348: the commands ever run here (a JSON list), for the tip
        self.place_path = place  # T-1338: the project left last, reopened by the next launch from home
        self.card_hidden = self.after_pick = self.view_title = None  # T-1762
        self.stopping = []  # T-1756: (name, pid) of the runners the last stop ended
        # T-1339: Foreman's own friction rounds (fmcode.noticed), first on home; a caller giving its own projects (a
        # test) gives its own noticed too, or none
        self.noticed_source = noticed if noticed is not None or projects is not None else _noticed
        if used:
            try:
                with open(used, encoding="utf-8") as f:
                    self.used = {x for x in json.load(f) if isinstance(x, str)}
            except (OSError, ValueError, TypeError):
                pass
            self.message = self._tip()
        self.spawn = spawn or self._spawn
        self.asker, self.asking, self.side = asker or self._ask_side, False, []  # T-1770: questions beside a run
        self.queued, self.queue_run, self.last_key = queued or self._queued, None, time.monotonic()  # T-1779
        self.root, self.opts = root or os.getcwd(), dict(opts or {})
        self.diff_source, self.history_dir = diff or self._git_diff, history
        self.choices_source = choices or self._choices
        # T-1330: the app's home (every Foreman project) and a project's view, read through fm
        self.projects_source, self.state_source = projects or fm_projects, project_state or fm_project_state
        self.project, self.view_back, self.next_root, self.prompt_back = None, "main", None, "main"
        self.child, self.events = None, queue.Queue()
        self.order, self.focus, self.scroll, self.follow, self.thoughts = [], 0, 0, True, False
        self.selected, self.expanded = None, set()
        self.view_lines, self.view_scroll, self.items, self.item_index = [], 0, [], 0
        self.view_opened, self.view_marks = 0.0, {}  # T-1312: when the diff view opened; its changed-word marks
        self.scrub = None  # T-1307: a replay's [events, how many are shown], stepped with the arrows
        self.tour = None  # T-1309: the tour's [when it began, how many of its events are shown]
        self._setup = None  # T-1333: the first-run checks shown on the welcome (None: not asked yet this launch)
        self.handoff = None  # T-1341: (argv, folder, task, CLI) the screen loop runs with the terminal handed over
        self.deck_cols, self.deck_rects, self.deck_ring, self.grow = 1, {}, None, None  # T-1345: the home's cards
        self.tickets, self.ticketing, self.ticket_held = [], False, False  # T-1350: requests for after this run
        self.undone, self.view_post = None, None  # T-1352: the commit undone this launch; the task a PR draft posts
        self.extra_labels, self.ext = list(EXT_LABELS), {}
        self.rule, self.rule_said = None, False  # T-1657: the [[rule]] whose layout shows; a rule error said once
        self.panel_strikes, self.panels_off = {}, {}  # T-1315: an extension panel's (frame, slow frames running); off
        self.motion, self.clock = Motion(score_durations(self.root)), time.monotonic  # T-1300, T-1626 (the score)
        self.confirm_key = None  # T-1348: (command, until) after a first q or s during a live run
        self.bell_seen = (0, False)  # T-1310: refusals counted and whether blocked, as of the last bell check
        self.game, self.game_t = None, 0.0  # T-1556: Breakout and the clock it was last stepped to
        self.glyphs = {}  # T-1512: the columns the terminal draws each PROBE glyph in (none measured: none assumed)
        # T-1572: the settings screen: every setting, its search, changed-only, the :set line and what it last did
        self.set_rows, self.set_problems, self.set_query, self.set_line = [], [], "", ""
        self.set_typing, self.set_modified, self.set_note, self.no_colour = "", False, None, False
        self.set_scope, self.run_settings, self.layer_said = "everywhere", {}, []  # T-1611: :set's scope, this run's
        self.set_pending = None  # T-1573: a previewed look: key, value, surface, the cfg before it, until when
        # T-1511: the layout editor's picked panel (column, row); the runner test going and whether one just ended
        self.lay_at, self.runner_test, self.runner_done, self.set_back = (0, 0), None, False, None
        self.runner_proc = None  # the CLI a runner test started: stop() ends its process group with the app
        self.tui = None  # T-1543: tui.toml as the app last read it ({path, text}, fmsettings.boot): watched for edits
        self.home_items, self.home_index = None, 0  # the home list a view over it goes back to
        # T-1344: when the home was last opened (seen_file keeps it across runs), its changed-since line, the typed jump
        self.home_seen, self.seen_file, self.home_note, self.jump = None, None, "", None
        self.commands = {"quit": self.quit, "new": self.new, "stop": self.stop, "resume": self.resume,
                         "search": self.open_search,  # T-1313
                         "help": self.help, "focus": self.next_focus, "up": lambda: self.move(-1),
                         "down": lambda: self.move(1), "page_up": lambda: self.move(-10),
                         "page_down": lambda: self.move(10), "expand": self.expand, "follow": self.follow_tail,
                         "diff": self.show_diff, "history": self.show_history, "model": self.show_picker,
                         "thoughts": self.toggle_thoughts, "projects": self.show_home,
                         "settings": self.show_settings, "hush": self.hush, "looks": self.show_looks,
                         "backlog": self.show_backlog,
                         "queue": self.work_queue, "tickets": self.show_tickets, "undo": self.undo,
                         "pr": self.draft_pr, "photo": self.take_photo, "breakout": self.open_breakout}
        self.size = None  # T-1546: the screen's size as last drawn, for the photo booth
        self.commands.update({name: self._ext_command(name, fn) for name, fn in EXT_COMMANDS.items()})

    def fps(self, t):
        """How often to draw: 30 while something eases or flashes, 12 while live work spins and pulses, 1 when the
        screen is still (4 with motion off and a run going, for its clock)."""
        mode = self.cfg.get("motion", "full")
        if self.mode == "breakout":  # T-1556: a game moves every frame, whatever the motion setting
            return 30
        if mode == "full" and self.grow and 0 <= t - self.grow[1] < GROW:  # T-1345: the project view growing
            return 30
        live = self.running() or self.state.status in ("planning", "running", "fixing")
        live = live or bool(self.tour and self.tour[1] < len(TOUR_RUN))  # T-1309: its next event comes on time
        if mode == "off":
            return 4 if live else 1
        if self.motion.busy(t):
            return 30
        return 12 if live and mode == "full" else 1

    def _ext_command(self, name, fn):
        def run():  # T-1315: one that raises or runs over COMMAND_BUDGET is said in Activity and switched off
            t0, why = time.monotonic(), None
            try:
                fn(self)
            except Exception as e:  # an extension's bug shows in the status bar; the screen stays up
                why = f"failed: {type(e).__name__}: {e}"
            took = time.monotonic() - t0
            if why is None and took > COMMAND_BUDGET:
                why = f"took {took * 1000:.0f} ms"
            if why:
                self.commands.pop(name, None)
                self.message = f"command {name} {why}; switched off"
                self.state.note("error", text=self.message)
        return run

    # -- keys
    def key(self, k):
        self.last_key = time.monotonic()  # T-1779: a digest says "while you were away" only when you were
        self._settings_expire()
        if self.set_pending:  # T-1573: any key, a paste too, keeps a previewed look, and does only that
            return self._settings_keep()
        if isinstance(k, tuple):  # T-1316: a paste: the prompt takes it whole, newlines kept; anywhere else nothing
            if self.mode == "prompt" and k[0] == "paste":
                text = "".join(c for c in k[1].replace("\t", " ") if c == "\n" or c.isprintable())
                self.line.insert(text[:max(0, PASTE_CAP - len(self.input))])  # T-1346: at the cursor
            elif self.mode == "settings" and self.set_typing == "bind":  # T-1511: a paste is no key to bind
                self._settings_bind(k)
            elif self.mode == "settings" and self.set_typing and k[0] == "paste":  # a colour, a list: one line
                for ch in " ".join(k[1].split())[:PASTE_CAP]:
                    if ch.isprintable():  # never Esc, Delete or Enter: a paste is text, not keys (T-1316)
                        self._settings_key(ch)
            return None
        if self.mode == "prompt":
            return self._prompt_key(k)
        if self.mode == "search":
            return self._search_key(k)
        if self.mode == "settings":
            return self._settings_key(k)
        if self.mode == "layout":
            return self._layout_key(k)
        if self.mode == "runners":
            return self._runners_key(k)
        if self.mode == "looks":
            return self._looks_key(k)
        if self.mode == "backlog":
            return self._backlog_key(k)
        if self.mode == "breakout":  # T-1556: the game plays its own keys; any other closes it and does its usual thing
            if self._game_key(k):
                return None
            self.mode = "main"
        if self.cfg["keys"].get(k) == "hush":  # T-1512: the run, its views, home and the keys poster (which it
            return self.hush()  # closes); the prompt and the settings list (above) keep their own keys
        if self.mode == "help":
            self.mode = "main"
            return None
        if self.mode in ("home", "project"):
            return self._home_key(k)
        if self.mode == "main" and self._card_key(k):  # T-1762
            return None
        if self.mode == "main" and self.scrub and k in SCRUB_KEYS and not self.running():  # T-1307
            return self._scrub_key(k)
        if (self.mode == "main" and k in SETUP_KEYS and k not in self.cfg["keys"] and self.state.status == "idle"
                and not self.running() and self.first_run()):  # T-1333: the welcome's fixes
            return self._setup_key(k)
        if self.mode == "tickets":  # T-1350
            return self._tickets_key(k)
        if self.mode in ("diff", "history", "picker"):
            return self._view_key(k)
        cmd = self.cfg["keys"].get(k)
        if cmd in ("quit", "stop") and self.running() and self.state.status in LIVE:  # T-1348: a live run ends only
            # on a second press within 2 s (a closed run whose process is still exiting quits at once)
            first = self.confirm_key
            if not (first and first[0] == cmd and self.clock() < first[1]):
                self.confirm_key = (cmd, self.clock() + CONFIRM_S)
                self.message = f"{key_name(k)} again within {CONFIRM_S:g} s to {cmd} the run"
                return None
        if not (self.confirm_key and self.confirm_key[0] == cmd):  # T-1352: a second press of the same key confirms
            self.confirm_key = None
        fn = self.commands.get(cmd)
        if fn:
            self.used.add(cmd)
        return fn() if fn else None

    def open_breakout(self):
        """B (T-1556): Breakout over the screen while the run's checks run; it closes when they stop."""
        if not checks_running(self.state):
            self.message = "Breakout opens while a run's checks run"
            return None
        from sampo import extras
        if self.game is None or self.game.over:
            self.game = extras.Breakout()
        self.game_t, self.mode = self.clock(), "breakout"
        return None

    def _game_key(self, k):
        """A key Breakout plays (←/→ or h/l move, Esc or B closes): True; any other is the app's."""
        if k in ("KEY_LEFT", "h"):
            self.game.move(-2)
        elif k in ("KEY_RIGHT", "l"):
            self.game.move(2)
        elif k == "\x1b" or self.cfg["keys"].get(k) == "breakout":
            self.mode = "main"
        else:
            return False
        return True

    def _list_cmd(self, k):
        """What a key runs in a list: LIST_KEYS first, then tui.toml's binding."""
        return LIST_KEYS.get(k) or self.cfg["keys"].get(k)

    def _view_key(self, k):
        cmd = self._list_cmd(k)
        step = {"up": -1, "down": 1, "page_up": -10, "page_down": 10}.get(cmd)
        if k in ("\x1b", "q") or cmd == {"diff": "diff", "history": "history", "picker": "model"}[self.mode]:
            self._back()
        elif cmd == "expand" and self.mode == "diff" and self.view_post:  # T-1352: post the PR draft shown
            tid = self.view_post
            try:
                out = self.fm_output(["pr", tid, "--post"])
            except (OSError, subprocess.SubprocessError) as e:
                out = f"couldn't run fm pr {tid} --post: {e}"
            self._back()
            self.message = _one_line(out)[:200] or f"posted {tid}'s description"
        elif step and self.mode == "diff":
            self.view_scroll = max(0, self.view_scroll + step)
        elif step:
            self.item_index = max(0, min(len(self.items) - 1, self.item_index + step))
        elif cmd == "expand" and self.items:
            item, self.mode = self.items[self.item_index], "main"
            if not isinstance(item, dict):  # a runner picked: back to the project it's for, if any
                self._back()
            self.view_back = "main"
            if isinstance(item, dict):
                self.replay(item["path"])
            else:
                kind, name = item
                self.opts.update(model=name if kind == "model" else None, worker=name if kind == "worker" else None)
                self.message = f"next run: {kind} {name}" + (self._last_run(name) if kind == "worker" else "")
                if self.after_pick == "resume":  # T-1762: m on a stall's card: carry the run on with this one
                    self.after_pick = None
                    self.resume()

    @property
    def input(self):  # T-1346: the request box's text, kept by its line editor
        return self.line.text

    @input.setter
    def input(self, text):
        self.line.set(text)

    def _prompt_key(self, k):
        if k in ("\n", "\r", "KEY_ENTER"):
            text, self.mode, self.prompt_back = self.input.strip(), "main", "main"
            self.line.remember(text)
            self.input = ""
            if text.startswith("/"):  # T-1346: an app command, not a run
                hit = self.palette(text)
                fn = self.commands.get(hit[0][0]) if hit else None
                if fn:
                    self.used.add(hit[0][0])
                    fn()
                else:
                    self.message = f"no app command matches {text}"
            elif text and self.ticketing:  # T-1350: a ticket on the rail, started when this run closes
                self.ticketing = False
                self.tickets.append(text)
                self.message = f"ticket {CIRCLED[min(len(self.tickets), len(CIRCLED)) - 1]} on the rail: it starts " \
                               "when this run closes (T lists them)"
            elif text and self.asking:  # T-1770: read-only, beside the run, which goes on
                self.asking = False
                self.ask(text)
            elif text:
                after, self.after = getattr(self, "after", None), None
                self.start([text], **({"after": after} if after else {}))  # T-1340
        elif k == "\x1b":
            self.input, self.next_root, self.asking, self.ticketing = "", None, False, False
            back, self.prompt_back = self.prompt_back, "main"
            if back == "project" and self.project is not None:
                self.mode, self.items, self.item_index = "project", self.project_items, self.project_index
            else:
                self.mode = "main"
        elif k == "\t" and (self.asking or self.ticketing) and not self.input.startswith("/"):  # T-1350
            self.asking, self.ticketing = self.ticketing, self.asking
        elif k == "\t":
            self.complete()
        else:
            self.line.key(k)

    def palette(self, text):
        """T-1346: [(command, label)] whose name starts with what follows the /, or whose label holds it."""
        q = text[1:].strip().lower()
        return [(cmd, label) for cmd, label in COMMAND_LABELS if cmd in self.commands
                and (cmd.startswith(q) or q and q in label.lower())]

    def tracked(self):
        """T-1346: the project's tracked files, read once (git ls-files, 20,000 at most) for @ completion."""
        if self.files is None:
            try:
                out = subprocess.run(["git", "-C", self.root, "ls-files"], capture_output=True, text=True, timeout=2)
                self.files = out.stdout.splitlines()[:20000] if out.returncode == 0 else []
            except (OSError, subprocess.SubprocessError):
                self.files = []
        return self.files

    def at_word(self):
        """The @word the cursor ends, or None."""
        word = self.input[:self.line.cur].rsplit(" ", 1)[-1]
        return word[1:] if word.startswith("@") else None

    def complete(self):
        """T-1346: Tab after @: the tracked file it starts (or what all the matches share)."""
        part = self.at_word()
        if part is None:
            return
        hits = [f for f in self.tracked() if f.startswith(part)]
        if not hits:
            self.message = f"no tracked file starts {part}"
            return
        done = hits[0] + " " if len(hits) == 1 else os.path.commonprefix(hits)
        self.line.insert(done[len(part):])

    def preview(self):
        """T-1346: the line under the request box: where Enter sends it."""
        if self.input.startswith("/"):
            hits = self.palette(self.input)
            return ("Enter runs: " + ", ".join(f"/{c} ({label})" for c, label in hits[:3])
                    + (f" (+{len(hits) - 3} more)" if len(hits) > 3 else "")) if hits else \
                "no app command matches"
        part = self.at_word()
        if part is not None:
            hits = [f for f in self.tracked() if f.startswith(part)][:3]
            return "Tab completes: " + ", ".join(hits) if hits else "no tracked file starts " + part
        if self.asking:  # T-1770
            return "Enter asks it beside the run, read-only (sampo smith --ask): the answer lands in Activity"
        runner = self.opts.get("model") or self.opts.get("worker") or "the default runner"
        return f"Enter starts a run in {os.path.basename(self.root.rstrip(os.sep)) or self.root} with {runner}"

    def open_search(self):
        """T-1313: / types a filter for the Activity log."""
        self.mode, self.search_draft = "search", self.search

    def _search_key(self, k):
        if k in ("\n", "\r", "KEY_ENTER"):
            self.mode, self.search = "main", self.search_draft.strip()
        elif k == "\x1b":
            self.mode, self.search, self.search_draft = "main", "", ""
        elif k in ("KEY_BACKSPACE", "\x7f", "\b"):
            self.search_draft = self.search_draft[:-1]
        elif len(k) == 1 and k.isprintable():
            self.search_draft = (self.search_draft + k)[:80]

    def focused(self):
        return self.order[self.focus % len(self.order)] if self.order else "log"  # the first frame too

    # -- commands
    def quit(self):
        self.done = True  # T-2030: first, so a runner CLI that spawns while stop() runs sees it and ends itself
        self.stop()
        if self.used_path:  # T-1348: what this launch used, for the next one's tip
            from sampo import core
            try:
                core.write_atomic(self.used_path, json.dumps(sorted(self.used | {"quit"})))
            except OSError:
                pass
        if self.place_path and self.project:
            from sampo import core
            try:
                core.write_atomic(self.place_path, json.dumps({"project": self.project.get("project"),
                                                                 "ts": round(time.time())}))
            except OSError:
                pass

    def reopen(self):
        """T-1338: home opened from no project goes back to the one left last, when its folder is still there."""
        try:
            with open(self.place_path or "", encoding="utf-8") as f:
                name = json.load(f).get("project")
        except (OSError, ValueError, AttributeError):
            return
        hit = next((x for x in self.items if name and x.get("project") == name and x.get("exists") is not False
                    and x.get("kind") != "noticed"), None)
        if hit:
            self.open_project(hit)
            self.message = f"welcome back to {name}"

    def _tip(self):
        """T-1348: one command this person has never run here, named once per launch."""
        keys = {cmd: k for k, cmd in self.cfg.get("keys", {}).items() if len(k) == 1 and k.isprintable()}
        for cmd, label in COMMAND_LABELS:
            if cmd not in self.used and cmd in keys and cmd not in TIP_SKIP:
                return f"tip: {keys[cmd]} {label}"
        return ""

    def new(self):
        if self.running():  # T-1770: a question about the code, answered beside the run
            self.mode, self.asking, self.ticketing, self.after = "prompt", True, False, None
            self.message = "a run is going: ask about the code beside it, or Tab: a ticket for after it"
        else:
            self.mode, self.after = "prompt", None  # T-1340: n is a fresh request; Enter after a close follows up

    def help(self):
        self.mode = "help"

    def hush(self):
        """T-1512: motion full → reduced → off → full, written as fm settings set writes it: tui.toml and the
        foreman-ui pane's option of the same name (the pane takes it on its next poll). A write refused (the guard, a
        lock) still hushes this screen, and says why."""
        from sampo import settings
        now = self.cfg.get("motion", "full")
        nxt = MOTIONS[(MOTIONS.index(now) + 1) % len(MOTIONS)] if now in MOTIONS else "full"
        try:
            done, refused = settings.set_key("motion", nxt, base=self._base())
        except Exception as e:  # a lock or a write that failed
            done, refused = [], [f"not written ({type(e).__name__}: {e})"]
        if any(s == "app" for s, _, _ in done):  # T-1543: as every write: what an outside edit merged in shows too
            self._settings_reload()
        self.cfg = dict(self.cfg, motion=nxt)
        if self.mode == "help":  # any key closes the poster, z too
            self.mode = "main"
        why = "; ".join(refused)
        self.message = f"motion: {nxt}" + ("" if not refused else f" ({why})" if any(s == "app" for s, _, _ in done)
                                          else f" on this screen only ({why})")

    def toggle_thoughts(self):
        self.thoughts = not self.thoughts
        self.message = "showing the model's own words" if self.thoughts else ""

    def next_focus(self):
        self.focus, self.scroll = self.focus + 1, 0

    def move(self, n):
        if self.focused() == "log":
            last = len(self.state.log) - 1
            self.selected = max(0, min(last, (last if self.selected is None else self.selected) + n))
            self.follow = self.selected == last
        else:
            self.scroll = max(0, self.scroll + n)

    def expand(self):
        if self.selected is not None:
            self.expanded ^= {self.selected}
        elif self.state.status == "closed" and self.state.task and not self.running():  # T-1340: Enter after a close
            self.after, self.mode = self.state.task, "prompt"
            self.message = f"a follow-up to {self.state.task}: its request, plan and changed files go with it"

    def follow_tail(self):
        self.follow, self.selected = True, None

    def ask(self, question):
        """T-1770: fm code --ask beside the run; its answer arrives as one aside event."""
        try:
            self.side.append(self.asker(question))
            self.message = "asking beside the run: the answer lands in Activity"
        except OSError as e:
            self.message = f"couldn't ask: {e}"

    def _ask_side(self, question):
        argv = [sys.executable, FM, "code", question, "--ask", "--json"]
        for k in ("model", "worker"):
            if self.opts.get(k):
                argv += [f"--{k}", str(self.opts[k])]
        p = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True,
                             env=dict(os.environ, FOREMAN_CODE_ATTENDED="1"))

        def read():  # only its answer reaches the app: the run's own events stay the run's
            answer = None
            for line in p.stdout:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("event") == "answer":
                    answer = str(e.get("text") or "")
            err = p.stderr.read()
            p.wait()
            self.events.put({"event": "aside", "question": question, "text": answer} if answer else
                            {"event": "aside", "question": question, "error": (err.strip().splitlines() or [""])[-1]})
        threading.Thread(target=read, daemon=True).start()
        return p

    def stop(self):
        for p in [self.runner_proc, *self.side]:  # T-1511: a runner test's CLI and T-1770's asks (each its own
            if p is not None and getattr(p, "poll", lambda: 0)() is None:  # session) end with the app
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
        if self.running():
            self.stopping = self._runners_of_child()  # T-1756: named on the way out
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except (OSError, AttributeError):
                pass
            self.message = "stopped: r resumes"

    # -- cards (T-1762)
    READ_ONLY_NEXT = re.compile(r"(?:fm|sampo) (?:task show T-\d+|code why(?: T-\d+)?|doctor|code models|code runners|code config)")

    def _card_key(self, k):
        """A key the card on screen takes (True), or False to let it through."""
        got = card(self.state, self)
        if not got:
            return False
        kind, _, rows, _ = got
        if k == "\x1b":
            self.card_hidden = (kind, rows[0][1])
        elif kind == "failure" and k in ("\n", "\r", "KEY_ENTER") and self.state.next:
            self.run_next(self.state.next)
        elif kind == "stall" and k == "w":
            self.card_hidden, self.message = (kind, rows[0][1]), "waiting: the turn goes on"
        elif kind == "stall" and k == "k":
            self.stop()
        elif k == "x" and kind in ("failure", "stall") and self.state.task:  # T-1341
            self.hand_off()
        elif kind == "stall" and k == "m":
            self.stop()
            try:
                self.child.wait(5)
            except (subprocess.TimeoutExpired, AttributeError):
                pass
            self.after_pick, self.view_back = "resume", "main"
            self.show_picker()
        else:
            return False
        return True

    def take_photo(self):
        """T-1546, the photo booth: F writes the frame on screen to Smith's photos folder, as an SVG and (with
        rsvg-convert installed) a PNG."""
        from sampo import core
        try:
            svg, png, note = photo(self, os.path.join(core.foreman_home(), "photos"), self.size or (120, 34),
                                   self.clock())
        except OSError as e:
            self.message = f"no photo: {e.strerror or e}"
            return None
        self.message = f"photo: {svg}" + (f" and .png" if png else f" ({note})")
        return None

    def _closed_commit(self):
        """T-1352: (task, commit) of the run on screen when it closed and committed, else None."""
        st = self.state
        m = re.search(r"\bcommitted ([0-9a-f]{7,40})\b", st.closed or "")
        return (st.task, m.group(1)) if st.status == "closed" and st.task and m and not self.running() else None

    def undo(self):
        """T-1352: u twice within CONFIRM_S on a closed run: a git revert of its commit (a new commit, nothing lost),
        and its history file says undone, so past runs show it."""
        got = self._closed_commit()
        if not got:
            self.message = "nothing to undo: u undoes a closed run's commit"
            return None
        tid, sha = got
        if self.undone == sha:
            self.message = f"{tid} is undone already"
            return None
        first = self.confirm_key
        if not (first and first[0] == "undo" and self.clock() < first[1]):
            self.confirm_key = ("undo", self.clock() + CONFIRM_S)
            self.message = f"u again within {CONFIRM_S:g} s to undo {tid} (git revert {sha})"
            return None
        self.confirm_key = None
        git = lambda *a: subprocess.run(["git", "-C", self.root or os.getcwd(), *a], capture_output=True, text=True,  # noqa: E731
                                        timeout=60, stdin=subprocess.DEVNULL)
        r = git("revert", "--no-edit", sha)
        if r.returncode:
            self.message = f"couldn't undo {tid}: {_one_line(r.stderr or r.stdout)[:160]}"
            return None
        new, self.undone = git("rev-parse", "--short", "HEAD").stdout.strip(), sha
        e = {"event": "undone", "text": f"{tid} undone: reverted by {new}", "task": tid, "revert": new, "ts": time.time()}
        path = os.path.join(self.history_dir, f"{tid}.events.jsonl") if self.history_dir else None
        if path and os.path.isfile(path):
            try:
                from sampo import core
                core.append_line(path, json.dumps(e) + "\n")
            except OSError:
                pass
        self.state.note("note", text=e["text"])
        self.message = f"undid {tid}: reverted by {new}"
        return None

    def draft_pr(self):
        """T-1352: P on a closed run: fm pr's description in the text view; Enter there posts it (fm pr ID --post),
        which fm pr allows only with the task's publish grant."""
        got = self._closed_commit()
        if not got:
            self.message = "no closed run: P drafts a closed run's pull request text"
            return None
        tid = got[0]
        try:
            out = self.fm_output(["pr", tid])
        except (OSError, subprocess.SubprocessError) as e:
            out = f"couldn't run fm pr {tid}: {e}"
        self.view_lines, self.view_marks = out.splitlines() or ["(no output)"], {}
        self.view_title = f"sampo pr {tid} · Enter posts it to its PR (needs the publish grant)"
        self.view_scroll, self.view_opened, self.view_back, self.mode = 0, self.clock(), "main", "diff"
        self.view_post = tid
        return None

    def run_next(self, nxt):
        """T-1762: a failure's next command, one key away: a resume carries the run on here; a read-only fm command's
        output opens in a view; anything else is named for the user's own shell (it may need their answer)."""
        if nxt.startswith(("fm code --resume", "sampo code --resume", "sampo smith --resume")):
            return self.resume()
        if not self.READ_ONLY_NEXT.fullmatch(nxt):
            self.message = f"run it in your shell: {nxt}"
            return None
        try:
            out = self.fm_output(shlex.split(nxt)[1:])
        except (OSError, subprocess.SubprocessError) as e:
            out = f"couldn't run {nxt}: {e}"
        self.view_lines, self.view_title, self.view_marks = out.splitlines() or ["(no output)"], nxt, {}
        self.view_scroll, self.view_opened, self.view_back, self.mode = 0, self.clock(), "main", "diff"
        return None

    def fm_output(self, argv):
        r = subprocess.run([sys.executable, FM, *argv], cwd=self.root, capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        return (r.stdout + r.stderr).strip()

    def _last_run(self, name):
        """T-1349: a hello for a picked runner: its newest run's end, or that it has none yet."""
        from sampo import smith
        try:
            got = smith.last_run_of(name)
        except Exception:
            return ""
        if not got:
            return " · no run with it yet"
        tid, how, when = got
        return f" · last: {how} {tid}, {duration(max(0, time.time() - when))} ago"

    def sweep(self):
        """T-1756: at the app's start, the runners a killed run left are stopped, and the status bar says so."""
        from sampo import smith
        try:
            stopped = smith.stop_survivors()
        except Exception:  # a sweep is housekeeping: the app opens whatever it finds
            stopped = []
        if stopped:
            self.message = f"stopped {len(stopped)} runner(s) a killed run left (pid {', '.join(map(str, stopped))})"
        return stopped

    def _runners_of_child(self):
        """T-1756: (name, pid) of each runner the running fm code registered (fmcode's code-procs entries it owns)."""
        from sampo import smith
        from sampo import ideas
        out, owner = [], str(getattr(self.child, "pid", ""))
        try:
            names = [n for n in os.listdir(smith._registry()) if n.isdigit()]
        except OSError:
            return out
        for n in names:
            try:
                with open(smith._registry(n)) as f:
                    if f.read().split()[1] != owner:
                        continue
                name = os.path.basename(os.fsdecode(ideas._cmdline(n)[0])) or "runner"  # T-1828: ps without /proc
            except (OSError, IndexError):
                continue
            out.append((name, int(n)))
        return out

    def stop_report(self, wait=3.0):
        """T-1756: after a stop, a line per runner it had: 'stopping NAME (pid N)… stopped'; one still there after
        `wait` s has its group killed and says so."""
        lines, end = [], time.monotonic() + wait
        for name, pid in self.stopping:
            while time.monotonic() < end and _alive(pid):
                time.sleep(0.05)
            how = "stopped"
            if _alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                    how = "stopped (killed)"
                except OSError:
                    how = f"still running: kill {pid}"
            lines.append(f"stopping {name} (pid {pid})… {how}")
        return lines

    def resume(self):
        if self.running() or not self.state.task:
            self.message = "a run is going" if self.state.task else "nothing to resume"
            return
        self.child = self.spawn([], dict(self.opts, resume=self.state.task))

    def show_diff(self):
        try:
            self.view_lines = (self.diff_source() or "").splitlines()
        except (OSError, subprocess.SubprocessError) as e:
            self.view_lines = [f"no diff: {e}"]
        self.view_scroll, self.mode, self.view_title = 0, "diff", None
        self.view_opened, self.view_marks = self.clock(), intraline(self.view_lines)  # T-1312

    # -- the app (T-1330): home over every Foreman project, a project's work and past runs
    def _back(self):
        """Close a view: to the project or home it was opened from, else the run's screen."""
        back, self.view_back, self.view_post = self.view_back, "main", None
        if back == "project" and self.project is not None:
            self.mode, self.items, self.item_index = "project", self.project_items, self.project_index
        elif back == "home" and self.home_items is not None:
            self.mode, self.items, self.item_index, self.home_items = "home", self.home_items, self.home_index, None
        elif back == "home":
            self.show_home()
        else:
            self.mode = "main"

    def show_home(self):
        try:
            found = list(self.projects_source())
        except Exception as e:  # fm isn't answering: say so, keep the screen
            found, self.message = [], f"can't list projects: {e}"
        self.items = sorted((x for x in found if isinstance(x, dict)), key=need_rank)  # T-1344
        if self.home_seen is None and self.seen_file:
            try:
                with open(self.seen_file) as f:
                    self.home_seen = float(f.read().strip())
            except (OSError, ValueError):
                pass
        self.home_note = _changed_line(self.items, self.home_seen)
        self.home_seen, self.jump = time.time(), None
        if self.seen_file:
            try:
                with open(self.seen_file, "w") as f:
                    f.write(str(self.home_seen))
            except OSError:  # the marker is a nicety: the home draws without it
                pass
        try:
            seen = [dict(n, kind="noticed") for n in (self.noticed_source or list)() if isinstance(n, dict)]
        except Exception:  # what Foreman noticed is extra: home draws without it
            seen = []
        self.items = seen + self.items
        self.item_index, self.mode = 0, "home"

    def open_project(self, proj):
        """A project's view: its active task, queue, inbox and blocked tasks, then its past runs; runs start there."""
        rows, self.project = [], proj
        if proj.get("exists") is False:
            rows.append({"kind": "error", "text": f"folder missing: {proj.get('root')} (nothing can run here)"})
        try:
            st = self.state_source(proj) or {}
        except Exception as e:  # its state can't be read: say why here, home still draws
            st, rows = {}, [{"kind": "error", "text": f"can't read {proj.get('project')}'s state: {e}"}]
        if isinstance(st.get("genesis"), dict):
            rows.append({"kind": "genesis", "genesis": st["genesis"]})
        if isinstance(st.get("active"), dict):
            rows.append({"kind": "task", "section": "active", "task": st["active"]})
        for sec in ("queue", "inbox", "blocked"):
            rows += [{"kind": "task", "section": sec, "task": t} for t in st.get(sec) or [] if isinstance(t, dict)]
        rows += [dict(r, kind="run") for r in history(st.get("code_dir"))]
        self.project_code = st.get("code_dir")  # the run's own folder and history move only when a run starts here
        self.project_items, self.project_index = rows, 0
        self.items, self.item_index, self.mode = rows, 0, "project"

    def _jump_key(self, k):
        """T-1344: / starts typing a project's name, each letter moves to the first project whose name starts with it
        (else whose folder is named so); Esc or Enter ends it. True when k was the jump's."""
        if self.jump is None:
            if k != "/":
                return False
            self.jump = ""
            return True
        if k in ("\x1b", "\n", "\r"):
            self.jump = None
        elif k in ("KEY_BACKSPACE", "\x7f", "\b"):
            self.jump = self.jump[:-1]
        elif isinstance(k, str) and len(k) == 1 and k.isprintable():
            self.jump += k
        else:
            return False
        rows = [(i, str(x.get("project")), str(x.get("root") or "")) for i, x in enumerate(self.items)
                if x.get("kind") != "noticed"]
        from sampo import core
        found = core.match_project(self.jump, [(s, r) for _, s, r in rows]) if self.jump else []
        if found:  # the first on the home that matches, so the one at the top of the ranking wins
            self.item_index = next(i for i, s, r in rows if s in {f[0] for f in found})
        return True

    def _home_key(self, k):
        if self.mode == "home" and self._jump_key(k):
            return None
        cmd = self._list_cmd(k)
        cols = self.deck_cols if self.mode == "home" else 1  # T-1345: up and down move a row of cards
        step = {"up": -cols, "down": cols, "page_up": -10, "page_down": 10}.get(cmd)
        if self.mode == "home" and k in ("KEY_LEFT", "KEY_RIGHT"):
            step = -1 if k == "KEY_LEFT" else 1
        item = self.items[self.item_index] if self.items else None
        if step:
            self.item_index = max(0, min(len(self.items) - 1, self.item_index + step))
            if self.mode == "project":
                self.project_index = self.item_index
        elif k == "\x1b" or cmd == "projects":
            if self.mode == "project":
                self.show_home()
            else:
                self.mode = "main"
        elif cmd == "quit":
            return self.quit()
        elif cmd == "settings":
            self.view_back = self.mode
            if self.mode == "home":  # back to this list where it was, not read again from the top
                self.home_items, self.home_index = self.items, self.item_index
            self.show_settings()
        elif self.mode == "home" and item and item.get("kind") == "noticed" and cmd == "expand":
            target = next((x for x in self.items if x.get("kind") != "noticed" and x.get("project") == item.get("project")),
                          None)
            if target:
                self.open_project(target)
        elif self.mode == "home" and item and item.get("kind") != "noticed" and cmd in ("expand", "new"):
            self.grow = (self.deck_rects.get(self.item_index), self.clock())  # T-1345: drawn growing from its card
            self.open_project(item)
            if cmd == "new":
                self.new_here()
        elif self.mode == "project" and cmd == "new":
            self.new_here()
        elif self.mode == "project" and cmd == "model":
            self.view_back = "project"
            self.show_picker()
        elif self.mode == "project" and item and item.get("kind") == "task" and cmd in ("expand", "resume"):
            self.run_task(item["task"])
        elif self.mode == "project" and item and item.get("kind") == "run" and cmd == "expand":
            self.mode = "main"
            self.replay(item["path"])
        elif self.mode == "project" and item and item.get("kind") == "run" and cmd == "resume" and \
                item.get("status") == "stopped" and self._here():  # T-1338: an unfinished run carried on there
            self.mode = "main"
            self.start([], resume=item["task"])
        return None

    def _here(self):
        """Whether a run can start in the open project; the next run's folder and history are set to it."""
        if self.running():
            self.message = "a run is going: s stops it"
            return False
        if (self.project or {}).get("exists") is False:
            self.message = f"{self.project.get('project')}'s folder is missing: nothing can run there"
            return False
        self.next_root = (str(self.project.get("root") or self.root), self.project_code)
        return True

    def run_task(self, t):
        """fm code --task ID in the open project's folder, with the runner picked by m."""
        if self._here():
            self.mode = "main"
            self.start(["--task", str(t.get("id"))])

    def new_here(self):
        """A new request typed for the open project; Esc goes back to its view."""
        if self._here():
            self.prompt_back = "project"
            self.new()

    def show_history(self):
        self.items, self.item_index, self.mode = history(self.history_dir), 0, "history"

    # -- taste (T-1537): fm looks calibrate's twelve pairs, one a screen; the answers go to the taste ledger at once
    def show_looks(self):
        from sampo import taste
        try:
            self.look_known, self.look_round = taste.looks(), taste.rounds_done()
            self.look_pairs, self.look_answers = taste.pairs(self.look_known, self.look_round), []
        except (OSError, ValueError) as e:  # no look cards to pair: said in the status bar, the screen stays
            self.message = f"taste: no calibration: {e}"
            return
        self._look_items()
        self.mode = "looks"

    def _look_items(self):
        """The pair now, no row highlighted (-1): Enter answers only once the user has moved to one."""
        k, (a, b) = self.look_known, self.look_pairs[len(self.look_answers)]
        self.items, self.item_index = [(f"1  {k[a]['title']}: {k[a]['line']}", "a"),
                                       (f"2  {k[b]['title']}: {k[b]['line']}", "b"), ("n  Neither", "n")], -1

    def _looks_key(self, k):
        cmd = self._list_cmd(k)
        step = {"up": -1, "down": 1}.get(cmd)
        ans = {"1": "a", "2": "b", "n": "n"}.get(k) or (
            self.items[self.item_index][1] if cmd == "expand" and self.item_index >= 0 else None)
        if k in ("\x1b", "q"):
            self.mode, self.message = "main", "taste: calibration stopped, nothing recorded"
        elif k in ("KEY_BACKSPACE", "\x7f", "\b"):  # back one answer: its pair is asked again
            if self.look_answers:
                self.look_answers.pop()
                self._look_items()
        elif step:
            self.item_index = max(0, min(len(self.items) - 1, self.item_index + step))
        elif ans:
            self.look_answers.append(ans)
            if len(self.look_answers) < len(self.look_pairs):
                return self._look_items()
            self.mode = "main"
            try:
                from sampo import taste
                taste.calibrate(self.look_answers, self.look_round)
                self.message = f"taste: {len(self.look_answers)} answers recorded (sampo looks shows the ranking)"
            except Exception as e:  # a busy lock, a full disk: said in the status bar, the app stays up
                self.message = f"taste: not recorded: {e}"
        return None

    # -- backlog (T-1856): fm inbox swipe's deferred pile; each verdict is applied the moment its key is pressed
    def show_backlog(self):
        from sampo import core
        from sampo import eco
        self.backlog_p = core.find_project(self.root)
        if not self.backlog_p:
            self.message = "backlog: this folder is not a Sampo project (sampo init makes it one)"
            return
        self.items, self.item_index, self.mode = eco.swipe_list(self.backlog_p), 0, "backlog"
        self.backlog_undo, self.backlog_kept = [], set()  # T-1871: u takes back the newest answer given in this view

    def _backlog_key(self, k):
        from sampo import eco
        step = {"up": -1, "down": 1}.get(self._list_cmd(k))
        verdict = {"r": "revive", " ": "keep", "d": "drop"}.get(k)
        if k in ("\x1b", "q"):
            self.mode = "main"
        elif step:
            self.item_index = max(0, min(len(self.items) - 1, self.item_index + step))
        elif k == "u":
            last = self.backlog_undo[-1] if self.backlog_undo else None
            ok, said = eco.swipe_undo(self.backlog_p, self.backlog_undo, self.backlog_kept)
            self.message = f"backlog: {said}"
            if ok:  # the pile read again less the cards kept here, the card taken back selected
                self.items = [x for x in eco.swipe_list(self.backlog_p) if x["id"] not in self.backlog_kept]
                self.item_index = next((i for i, x in enumerate(self.items) if x["id"] == last[0]), 0)
        elif verdict and self.items:
            try:
                said = eco.swipe_apply(self.backlog_p, f"{self.items[self.item_index]['id']}:{verdict}",
                                         self.backlog_undo)
            except Exception as e:  # changed elsewhere since the list was read, a busy lock: said, the app stays up
                from sampo import cli as scli
                self.message = f"backlog: {e}"
                if isinstance(e, scli.UsageError):  # not deferred now: the stale row goes, a busy lock keeps it
                    del self.items[self.item_index]
                    self.item_index = max(0, min(self.item_index, len(self.items) - 1))
                return None
            self.message = f"backlog: {said[0]}"
            if verdict == "keep":
                self.backlog_kept.add(self.items[self.item_index]["id"])
            del self.items[self.item_index]
            self.item_index = max(0, min(self.item_index, len(self.items) - 1))
        return None

    def show_picker(self):
        try:
            self.items = list(self.choices_source())
        except Exception as e:  # a model server that doesn't answer: say so, keep the screen
            self.items, self.message = [], f"no models: {e}"
        self.item_index, self.mode = 0, "picker"

    # -- settings (T-1572): fmsettings' one schema, shared with the Claude Code pane, written by fm settings' writer
    def show_settings(self):
        self.set_query, self.set_line, self.set_typing, self.set_modified, self.set_note = "", "", "", False, None
        self.mode = "settings_loading"  # not "settings" yet: the list before it isn't a selection to keep
        self._settings_load()
        self.mode = "settings"

    def _settings_load(self, keep=None):
        """Every setting's value now (both files read again), grouped; a colour's default is its theme's own."""
        from sampo import settings
        problems = []
        try:
            rows = settings.rows(problems=problems)
        except Exception as e:  # a file that can't be read at all: said on the screen, the list empty
            rows, problems = [], [("app", f"can't read the settings: {type(e).__name__}: {e}")]
        now = {r["key"]: r["value"] for r in rows if r["surface"] == "app"}
        base = preset_theme(now.get("theme", "night"), now.get("palette", "default"))
        order = list(dict.fromkeys(r["group"] for r in rows))  # the user's own key bindings join the keys
        where = self.cfg.get("scopes") or {}  # T-1611: a narrower scope that sets a row's setting is marked on it
        rows = [dict(r, scope=where[settings._norm(r["key"])]) if r["surface"] == "app"
                and settings._norm(r["key"]) in where else r for r in rows]
        self.set_rows = sorted((dict(r, default=base.get(r["key"][7:], r["default"])) if r["group"] == "colors" else r
                                for r in rows), key=lambda r: order.index(r["group"]))
        self.set_problems = problems
        self._settings_filter(keep)

    def _settings_row(self):
        r = self.items[self.item_index] if 0 <= self.item_index < len(self.items) else None
        return None if r is None or "head" in r else r

    def _settings_filter(self, keep=None):
        """The rows the search (every word in the key) and changed-only leave, a header over each group; the selection
        stays on its setting while it's listed."""
        cur = self._settings_row() if self.mode == "settings" else None
        keep = keep or (cur and (cur["surface"], cur["key"]))
        words = self.set_query.lower().split()
        out, head = [], None
        for r in self.set_rows:
            if (self.set_modified and r["value"] == r["default"]) or not all(w in r["key"].lower() for w in words):
                continue
            if head is None or head["head"] != r["group"]:
                head = {"head": r["group"], "surface": r["surface"], "count": 0}
                out.append(head)
            head["count"] += 1
            out.append(r)
        rows = [i for i, r in enumerate(out) if "head" not in r]
        self.items = out
        self.item_index = next((i for i in rows if (out[i]["surface"], out[i]["key"]) == keep), rows[0] if rows else 0)

    def _settings_step(self, n):
        """Up or down n settings; group headers are passed over."""
        rows = [i for i, r in enumerate(self.items) if "head" not in r]
        if rows:
            want = self.item_index + n
            self.item_index = min(rows, key=lambda i: (abs(i - want), (i - want) * n < 0))

    def _settings_key(self, k):
        if self.set_typing == "bind":  # T-1511: whatever key comes next is the one bound, an arrow too
            return self._settings_bind(k)
        cmd = self._list_cmd(k)
        moves = {"KEY_UP": -1, "KEY_DOWN": 1, "KEY_PPAGE": -10, "KEY_NPAGE": 10}
        step = moves.get(k) if self.set_typing else {"up": -1, "down": 1, "page_up": -10, "page_down": 10}.get(cmd)
        if step:
            return self._settings_step(step)
        if self.set_typing:  # the search or the :set line takes every printable key
            search, done = self.set_typing == "search", k in ("\n", "\r", "KEY_ENTER", "\x1b")
            text = self.set_query if search else self.set_line
            if done:
                self.set_typing, self.set_line = "", ""
                if k == "\x1b" and search:
                    text = ""
                elif not search:
                    return self._settings_set(text) if k != "\x1b" else None
            elif k in ("KEY_BACKSPACE", "\x7f", "\b"):
                text = text[:-1]
            elif len(k) == 1 and k.isprintable():
                text = (text + k)[:500]
            if search:
                self.set_query = text
                self._settings_filter()
            else:
                self.set_line = text
            return None
        r = self._settings_row()
        if k in ("\x1b", "q") or cmd == "settings":
            self._back()
        elif k == "/":
            self.set_typing = "search"
        elif k == "m":
            self.set_modified = not self.set_modified
            self._settings_filter()
        elif k == ":":
            self.set_typing, self.set_line = "set", ""
        elif k == "b":  # T-1511: the next key pressed runs this row's command
            if r and r["group"] == "keys" and r["value"] in COMMANDS:
                self.set_typing, self.set_note = "bind", None
            else:
                self.set_note = ("b binds a key to a command: pick a row under KEYS that runs one", "bad")
        elif k == "s":  # T-1611: where the next :set goes: everywhere, this project, this run
            self.set_scope = SCOPES[(SCOPES.index(self.set_scope) + 1) % len(SCOPES)]
            self.set_note = (f"a :set now goes to {SCOPE_WORDS[self.set_scope]} (s changes it)", "ok")
        elif k == "l":
            self.set_note, self.lay_at, self.mode = None, (0, 0), "layout"
        elif k == "r":
            self.show_runners()
        elif k in ("[", "]") and r:  # T-1661: the selected number turned one detent, through :set (scope, pins)
            from sampo import settings
            try:
                new = settings.detent(r, r["value"], 1 if k == "]" else -1)
            except settings.SettingError as e:
                self.set_note = (str(e), "bad")
                return None
            return self._settings_set(f"set --surface {r['surface']} {r['key']} {new}")
        elif cmd == "expand" and r:  # the :set line for this setting, its value ready to change
            both = sum(x["key"] == r["key"] for x in self.set_rows) > 1
            where = "--surface " + r["surface"] + " " if both else ""
            self.set_typing, self.set_line = "set", f"set {where}{r['key']} {_shown(r['value'], r['surface'])}"
        return None

    def _settings_set(self, line):
        """`set KEY VALUE [--surface app|plugin]`, as fm settings set takes it (the same writer; a value may be quoted
        as in TOML). An app setting applies at once: the app reads its tui.toml again."""
        import re
        import tomllib
        from sampo import settings
        m = re.search(r"(?:^|\s)--scope[= ](\S*)", line)  # T-1611: else the scope s picked
        scope = m.group(1) if m else self.set_scope
        line = line[:m.start()] + " " + line[m.end():] if m else line
        m = re.search(r"(?:^|\s)--surface[= ](\S*)", line)
        surface = m.group(1) if m else None
        words = (line[:m.start()] + " " + line[m.end():] if m else line).strip()
        if words == "set" or words.startswith("set "):
            words = words[3:].strip()
        key, _, value = words.partition(" ")
        value = value.strip()
        if surface not in (None, "app", "plugin") or not key or not value:
            self.set_note = ("set KEY VALUE [--surface app|plugin]", "bad")
            return None
        try:  # "off" as TOML writes it is the word off; a list or a table stays text, as fm settings set takes it
            got = tomllib.loads(f"v = {value}")["v"]
            value = got if isinstance(got, str) else value
        except (tomllib.TOMLDecodeError, RecursionError):
            pass
        if not settings.find(key, surface):
            self.set_note = (f"{key}: no such setting{f' in the {surface}' if surface else ''}", "bad")
            return None
        if scope not in SCOPES or scope != "everywhere" and (surface == "plugin" or not settings.find(key, "app")):
            self.set_note = (f"--scope takes {', '.join(SCOPES)}; this project and this run hold the app's settings",
                             "bad")
            return None
        if (self.cfg.get("scopes") or {}).get(settings._norm(key)) == "pin" and surface != "plugin":
            self.set_note = (f"{key}: pinned by {policy_path(self.root)}: the repo's policy holds it", "bad")
            return None
        if scope != "everywhere":
            return self._settings_scoped(scope, key, value)
        if key in PREVIEW_KEYS and surface in (None, "app"):
            return self._settings_preview(key, value, surface)
        return self._settings_write(key, value, surface)

    def _settings_scoped(self, scope, key, value):
        """T-1611: a :set for this project (written to project_settings_path as tui.toml is written) or this run
        (checked as a write is, held in memory, gone when the app quits)."""
        from sampo import settings
        try:
            if scope == "project":
                path = project_settings_path(self.root)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                value, _ = settings.set_app(key, value, path=path)
            else:
                value = settings._check(settings.find(key, "app")[0], value)  # nothing is written: no guard
                table, name = settings._split(key)
                (self.run_settings.setdefault(table, {}) if table else self.run_settings)[name] = value
        except Exception as e:
            self.set_note = (_escaped(f"{scope}: {e}" if isinstance(e, settings.SettingError) else
                                      f"{scope}: {key}: not set ({type(e).__name__}: {e})"), "bad")
            return None
        self._settings_reload()
        self.set_note = (_escaped(f"{SCOPE_WORDS[scope]}: {key} = {_shown(value, 'app')}"
                                  + (f" in {path}" if scope == "project" else ", gone when the app quits")), "ok")
        self._settings_load(keep=("app", key))
        return None

    def _layer(self, cfg):
        """T-1611: cfg with this project's settings, this run's and the repo's pins over it; a problem said once."""
        try:
            cfg, problems = layered(cfg, self.root, self.run_settings)
        except Exception as e:  # a layer that chokes: tui.toml's look stays, said
            problems = [f"the project's or the policy's settings weren't read ({type(e).__name__}: {e})"]
        if problems and problems != self.layer_said:
            self.message, self.layer_said = _escaped("; ".join(problems))[:400], problems
        return cfg

    def _settings_preview(self, key, value, surface):
        """T-1573: a look shown at once on the live config, so the screen behind the overlay repaints; written (on
        every surface the :set names) only when kept: any key within PREVIEW_SECONDS, else the screen goes back."""
        from sampo import settings
        try:  # the write's checks, the guard's too: what it would refuse is refused now, not after the keep
            value, cfg = settings.preview_app(key, value)
            cfg = self._layer(cfg)  # T-1611: the narrower scopes still over it
        except Exception as e:
            self.set_note = (f"app: {e}" if isinstance(e, settings.SettingError) else
                             f"app: {key}: can't show it ({type(e).__name__}: {e})", "bad")
            return None
        # time.monotonic doesn't count a suspend: a sleep holds the preview longer (safe: nothing is written)
        self.set_pending = {"key": key, "value": value, "surface": surface, "cfg": self.cfg,
                            "until": self.clock() + PREVIEW_SECONDS, "base": self._base()}  # T-1543: what it showed
        self.cfg, self.set_note = dict(cfg, theme=_mono(), preset="mono") if self.no_colour else cfg, None
        return None

    def _settings_keep(self):
        p, self.set_pending = self.set_pending, None
        self.cfg = p["cfg"]  # the look before it, unless the write takes (a guard's refusal leaves the file as it was)
        return self._settings_write(p["key"], p["value"], p["surface"], p["base"])

    def _settings_expire(self):
        """A preview not kept in time: the look before it (nothing was written); whether one went back."""
        p = self.set_pending
        if not p or self.clock() < p["until"]:
            return False
        self.cfg, self.set_pending = p["cfg"], None
        self.set_note = (f"{p['key']} = {_shown(p['value'], 'app')} not kept: no key within {PREVIEW_SECONDS} s; "
                         "nothing written", "warn")
        return True

    def _settings_write(self, key, value, surface, base=None):
        """T-1543: `base`, the tui.toml text the change was made on (a preview's), else the one the app last read: a
        key another program changed since is refused, and the screen takes the file as it is now."""
        from sampo import settings
        try:
            done, refused = settings.set_key(key, value, surface, base=self._base() if base is None else base)
        except Exception as e:  # a lock or a write that failed: said, the screen stays
            done, refused = [], [f"{key}: not written ({type(e).__name__}: {e})"]
        if any(s == "app" for s, _, _ in done):
            self._settings_reload()
        elif refused:
            self._watch_tui(now=True)
        said = [f"{s}: {key} = {_shown(v, s)} ({'now' if s == 'app' else 'the pane reads it within seconds'})"
                + "".join(f"; now skipped: {w}" for w in warn) for s, v, warn in done]
        over = settings.overrides(key) if any(s == "app" for s, _, _ in done) else []  # T-1656: never silent
        self.set_note = (_escaped("; ".join(said + over + refused)), "bad" if refused else "warn" if over else "ok")
        self._settings_load(keep=(done[0][0] if done else surface or "app", key))
        return None

    def _settings_reload(self):
        from sampo import settings
        try:
            cfg, _ = load_config()
            cfg = self._layer(cfg)  # T-1611: this project's, this run's and the pins over tui.toml
            why = settings.render_error(cfg)  # T-1543: an outside edit the write merged in may not draw
        except Exception as e:  # T-1657: one load_config chokes on: the running look stays, said
            cfg, why = None, f"{type(e).__name__}: {e}"
        if why:
            self.message = settings._safe(f"the app can't draw with tui.toml ({why}): the running look kept")
        else:
            self.cfg = dict(cfg, theme=_mono(), preset="mono") if self.no_colour else cfg
        self._own_write()

    def _own_write(self):
        """T-1543: tui.toml after the app's own write is what it has seen: not an outside edit for the watch."""
        from sampo import settings
        if self.tui:
            try:
                self.tui["text"] = settings._read_app(self.tui["path"])
            except settings.SettingError:
                pass

    def _base(self):
        """T-1543: the tui.toml text the app last read and parsed (None: not watched)."""
        return self.tui.get("valid", self.tui["text"]) if self.tui else None

    def _watch_tui(self, now=False):
        """T-1543: tui.toml written by another program while the app runs (an editor, fm settings in a shell), read
        again once a second: applied when the app can draw with it, the running look kept when it can't; said either
        way, never written back. A previewed look stays up: the look it goes back to is the file's. Whether it
        changed."""
        from sampo import settings
        if not self.tui or (not now and self.clock() < self.tui.get("next", 0)):
            return False
        self.tui["next"] = self.clock() + 1
        got = settings.reread(self.tui)
        if not got:
            return False
        cfg, self.message = got
        if cfg is not None:
            cfg = self._layer(cfg)  # T-1611
            cfg = dict(cfg, theme=_mono(), preset="mono") if self.no_colour else cfg
            if self.set_pending:
                self.set_pending["cfg"] = cfg
            else:
                self.cfg = cfg
        if self.mode == "settings":
            self.set_note = (self.message, "ok" if cfg is not None else "bad")
            self._settings_load()
        return True

    # -- T-1511: the settings screen's editors: a key captured, panels moved by keys, a runner tested
    def _settings_bind(self, k):
        """The key pressed after b, bound to the selected row's command through fm settings' writer (undo covers it);
        Esc cancels, and a key the app keeps for itself or one tui.toml can't name is refused, with why."""
        r, self.set_typing = self._settings_row(), ""
        name = key_name(k) if isinstance(k, str) and k else None
        if k == "\x1b" or not r:
            self.set_note = ("nothing bound", "dim")
        elif not name or k not in (parse_key(name) or ()):
            self.set_note = (f"{'a paste' if isinstance(k, tuple) else _escaped(str(k))} can't be named in tui.toml "
                             f"(a letter, ctrl+x, f1-f12, {', '.join(KEY_ALIASES)}): nothing bound", "bad")
        elif self._kept(k):
            self.set_note = (f"{name} is the app's own key ({self._kept(k)}): nothing bound; pick another", "bad")
        elif self._last(k, name) not in (None, r["value"]):
            self.set_note = (f"{name} is the last key for {self._last(k, name)}: bind another key to it first; "
                             "nothing bound", "bad")
        else:
            was = self.cfg["keys"].get(k)
            self._settings_write(f"keys.{name}", r["value"], "app")
            if was and was != r["value"] and self.set_note[1] == "ok":
                self.set_note = (f"{self.set_note[0]}; it ran {was} before", "ok")

    def _kept(self, k):
        """What a key the app keeps for itself does (RESERVED, or a KEPT command tui.toml bound it to), or None."""
        cmd = self.cfg["keys"].get(k)
        return RESERVED.get(k) or (cmd if cmd in KEPT else None)

    def _last(self, k, name):
        """The LAST command no key but `k` runs (enter's two keys are one name: neither counts for the other), or
        None."""
        cmd, mine = self.cfg["keys"].get(k), parse_key(name)
        return cmd if cmd in LAST and not any(c == cmd for x, c in self.cfg["keys"].items() if x not in mine) else None

    def _lay_pos(self):
        """The picked panel, kept inside the columns as they are now (an undo elsewhere can shorten them)."""
        cols = self.cfg["layout"]["columns"]
        c = max(0, min(self.lay_at[0], len(cols) - 1))
        return c, max(0, min(self.lay_at[1], len(cols[c]) - 1))

    def _layout_key(self, k):
        """←↑↓→ (or h j k l) pick a panel; H and L move it a column left or right, K and J a row up or down."""
        cols, (c, i) = self.cfg["layout"]["columns"], self._lay_pos()
        pick = {"h": (-1, 0), "KEY_LEFT": (-1, 0), "l": (1, 0), "KEY_RIGHT": (1, 0), "k": (0, -1), "KEY_UP": (0, -1),
                "j": (0, 1), "KEY_DOWN": (0, 1)}.get(k)
        move = {"H": (-1, 0), "L": (1, 0), "K": (0, -1), "J": (0, 1)}.get(k)
        if k in ("\x1b", "q"):
            self._settings_back(("app", "layout.columns"))
        elif pick:
            c = max(0, min(len(cols) - 1, c + pick[0]))
            self.lay_at = (c, max(0, min(len(cols[c]) - 1, i + pick[1])))
        elif move:
            self._layout_move(c, i, *move)

    def _layout_move(self, c, i, dc, di):
        """The panel at column c, row i moved; layout.columns and layout.heights (each panel keeps its own) written as
        one change, so one fm settings undo puts the move back. A column keeps one panel at least."""
        from sampo import settings
        try:
            got = settings.layout_moved(self.cfg["layout"], c, i, dc, di)
        except settings.SettingError as e:  # a column's only panel
            self.set_note = (str(e).replace("(set ", "(:set "), "bad")
            return None
        if got is None:
            return None  # the edge: nowhere to go
        cols, hts, name, to, at = got
        try:  # one write: both or neither
            done = settings.set_apps([("layout.columns", cols), ("layout.heights", hts)], base=self._base())
            self.lay_at = (to, at)
            self.set_note = (f"{name} moved: column {to + 1}, row {at + 1}" + "".join(
                f"; now skipped: {w}" for _, _, warn in done for w in warn), "ok")
        except Exception as e:  # the guard, a value refused, a lock or a write that failed: nothing written
            self.set_note = (f"app: {e}" if isinstance(e, settings.SettingError) else
                             f"not written ({type(e).__name__}: {e})", "bad")
        finally:  # the screen as the file is, whatever happened
            self._settings_reload()
        return None

    def _settings_back(self, keep=None):
        """From an editor back to the settings list, its values read again, the setting it was opened from selected;
        the editor's note goes with it."""
        self.mode, self.set_note = "settings_loading", None  # the runners' list isn't a selection to keep
        self._settings_load(keep)
        self.mode = "settings"

    def show_runners(self):
        """Every runner of the registry and code.toml (only those: a test never takes a typed command)."""
        from sampo import smith
        self.set_back, note = self._settings_row(), None
        try:
            rs = smith.all_runners()
        except smith.CodeError as e:  # the built-ins still list, with what's wrong in code.toml
            rs, note = {n: dict(r) for n, r in smith.RUNNERS.items()}, (str(e), "bad")
        pings = smith.ping_cache()  # T-1335: each runner's last ping, as its lamp
        self.items = [{"name": n, "work": bool(r.get("work")), "installed": bool(smith._installed(n, r)),
                       "ping": pings.get(n) if isinstance(pings.get(n), dict) else None} for n, r in rs.items()]
        self.item_index, self.set_note, self.mode = 0, note, "runners"

    def _runners_key(self, k):
        cmd = self._list_cmd(k)
        step = {"up": -1, "down": 1, "page_up": -10, "page_down": 10}.get(cmd)
        if k in ("\x1b", "q"):
            self._settings_back(self.set_back and (self.set_back["surface"], self.set_back["key"]))
        elif step:
            self.item_index = max(0, min(len(self.items) - 1, self.item_index + step))
        elif cmd == "expand" and self.items:
            self._runner_test(self.items[self.item_index])
        elif k == "p" and self.items:
            self._runner_test(self.items[self.item_index], ping=True)

    def _runner_test(self, item, ping=False):
        """fmcode.test_runner on a thread (it waits up to RUNNER_TEST_SECONDS), or with ping fmcode.ping_runner (up
        to PING_SECONDS, kept in the ping file): the screen keeps drawing, and poll redraws when the result is in.
        One at a time."""
        from sampo import smith
        if self.runner_test is not None and self.runner_test.is_alive():
            self.set_note = ("a test is running: its result comes first", "warn")
            return

        def run():
            def spawned(p):
                self.runner_proc = p
                if self.done and p.poll() is None:  # T-2030: the quit landed before this; stop() saw no CLI
                    with contextlib.suppress(OSError):
                        os.killpg(p.pid, signal.SIGKILL)
            try:
                if ping:
                    got = smith.ping_runner(item["name"], on_spawn=spawned)
                    smith.ping_cache(got)
                else:
                    got = smith.test_runner(item["name"], cwd=self.root, on_spawn=spawned)
            except Exception as e:  # not a runner with a command (CodeError), or anything else: its result
                why = str(e) if isinstance(e, smith.CodeError) else f"{type(e).__name__}: {e}"
                got = {"name": item["name"], "state": "failed", "said": why} if ping else {"ok": False, "said": why}
            item.update(**({"ping": got, "pinging": False} if ping else {"result": got, "testing": False}))
            self.runner_done = True
        item.update(**({"pinging": True, "result": None} if ping else {"testing": True, "result": None}))
        self.set_note = None
        self.runner_test = threading.Thread(target=run, daemon=True)
        self.runner_test.start()

    def replay(self, path):
        events = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict):
                        events.append(e)
        except OSError as e:
            self.message = f"can't read {path}: {e}"
            return
        self.motion, self.scrub = Motion(), [events, 0]
        self._scrub_to(len(events))
        self.message = (f"viewing {self.state.task or 'a past run'}"
                        + (f" · ←/→ step through its {len(events)} events" if len(events) > 1 else ""))

    def setup_checks(self):
        return setup_checks(self.root)

    @staticmethod
    def live(argv, cwd):
        """T-1341: a CLI the person drives, in this terminal; Ctrl-C is its own (the app ignores it meanwhile)."""
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return subprocess.run(argv, cwd=cwd, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL)
                                  ).returncode
        finally:
            signal.signal(signal.SIGINT, old)

    def hand_off(self):
        """T-1341: x on a stall's or a failure's card: the run stops and its task goes to a live CLI with its brief;
        the screen loop runs it (run_handoff), then the checks run again."""
        st = self.state
        cli = live_cli(st.worker or self.opts.get("worker"))
        if not st.task or not cli:
            self.message = ("no task to hand over yet" if not st.task else
                            "no interactive CLI found (claude, codex or gemini on PATH)")
            return None
        if self.running() or st.status in LIVE:
            self.stop()
            try:
                self.child.wait(5)
            except (subprocess.TimeoutExpired, AttributeError):
                pass
        name, template = cli
        prompt = brief_prompt(st)
        self.handoff = ([prompt if w == "{prompt}" else w for w in shlex.split(template)], self.root or os.getcwd(),
                        st.task, name)
        self.message = f"handing {st.task} to {name}"
        return None

    def run_handoff(self):
        argv, root, tid, name = self.handoff
        self.handoff = None
        try:
            self.live(argv, root)
        except OSError as e:
            self.message = f"couldn't start {name}: {e}"
            return
        self.start([], recheck=tid)
        self.message = f"back from {name}: re-running {tid}'s checks"

    def first_run(self):
        """T-1333: the checks the welcome shows before this project's first run; [] once it has a past run."""
        if self._setup is None:
            try:
                past = bool(self.history_dir) and any(n.endswith(".events.jsonl") for n in os.listdir(self.history_dir))
            except OSError:
                past = False
            self._setup = [] if past else self.setup_checks()
        return self._setup

    def _setup_key(self, k):
        if k == "e":  # the first question: read-only, beside no run
            self.mode, self.asking = "prompt", True
            self.line.text = "explain this repo"
            self.line.cur = len(self.line.text)
            return None
        try:
            self.message = (self._link_fm if k == "l" else self._init_project)()
        except OSError as e:
            self.message = f"couldn't: {e}"
        self._setup = None  # asked again on the next frame
        return None

    def _link_fm(self):
        from sampo import setup
        made = setup.ensure_fm_link()
        return (f"linked {made}: a new terminal finds fm" if made else
                "~/.local/bin/fm is there already, or someone else's: fm doctor says which")

    def _init_project(self):
        from sampo import core
        p = core.find_project(self.root or os.getcwd(), create=True)
        return f"{p.slug} is a Sampo project now" if p else "not a project: no git repository here"

    def start_tour(self):
        """T-1309: the sample run played on this app's clock, a caption over it naming each panel."""
        self.state, self.motion, self.tour, self.message = State(), Motion(), [self.clock(), 0], "the tour: q leaves"

    def _tour_tick(self):
        start, fed = self.tour
        due = min(len(TOUR_RUN), int((self.clock() - start) / TOUR_GAP) + 1)
        for e in TOUR_RUN[fed:due]:
            self.feed(e)
        self.tour[1] = max(fed, due)
        return due > fed

    @property
    def caption(self):
        return tour_caption(self.tour[1]) if self.tour else None

    def _scrub_to(self, n):
        """T-1307: the replay as it stood after its first n events (State.apply, the one reducer, from the start)."""
        events = self.scrub[0]
        n = max(min(1, len(events)), min(len(events), n))
        st = State()
        for e in events[:n]:
            st.apply(e)
        self.scrub[1] = n
        self.state, self.selected, self.expanded, self.follow = st, None, set(), True
        if n:
            kind = "".join(ch for ch in str(events[n - 1].get("event") or "?") if ch.isprintable())[:30]
            self.message = f"◂▸ {n}/{len(events)} {kind}"  # short: the status bar's hints take most of its width

    def _scrub_key(self, k):
        n, end = self.scrub[1], len(self.scrub[0])
        self._scrub_to({"KEY_LEFT": n - 1, "KEY_RIGHT": n + 1, "KEY_HOME": 1, "KEY_END": end}[k])

    def _git_diff(self):
        """The run's changes step by step: each file under the step that first changed it, a new file in full; after
        the close, the commit's. fm code's judge's git, with no external diff or textconv: nothing a worker planted in
        the repo's config runs when d is pressed."""
        from sampo import smith
        git = lambda *a: os.fsdecode(smith._git(self.root, *a).stdout)
        closed = self.state.status == "closed"
        base = ["show", "--format=", "--no-ext-diff", "--no-textconv", "HEAD"] if closed else [
            "diff", "--no-ext-diff", "--no-textconv", "HEAD"]
        groups, seen = [], set()
        for n in sorted(self.state.files):
            new = [f for f in self.state.files[n] if f not in seen]
            seen.update(new)
            groups += [(n, new)] if new else []
        if not groups:
            return git(*base)
        out = []
        for n, files in groups:
            out.append(f"── step {n}: {self.state.steps[n - 1]['text'] if 0 < n <= len(self.state.steps) else ''} ")
            for f in files:
                d = git(*base, "--", f)
                if not d and not closed and os.path.isfile(os.path.join(self.root, f)):  # new, so untracked
                    d = git("diff", "--no-ext-diff", "--no-textconv", "--no-index", "--", os.devnull, f)
                out += d.rstrip("\n").splitlines()
        return "\n".join(out)

    def _choices(self):
        from sampo import smith
        from sampo import models
        out = [("model", p.get("name")) for p in models.profiles() if p.get("name")]
        try:
            runners = smith.all_runners()
        except smith.CodeError as e:  # T-1395: the models still list, with what's wrong in code.toml
            self.message = str(e)
            return out
        return out + [("worker", w) for w, r in runners.items()  # T-1330: code.toml's runners too
                      if r.get("work") and smith._installed(w, r)]

    # -- the child
    def running(self):
        return self.child is not None and getattr(self.child, "poll", lambda: 0)() is None

    def bell_due(self):
        """T-1310: whether to ring now: once each time the run turns blocked or the guard refuses another action;
        never with motion off."""
        refused = sum(1 for a in self.state.actions if a.get("result") == "refused")
        blocked = self.state.status == "blocked"
        due = refused > self.bell_seen[0] or blocked and not self.bell_seen[1]
        self.bell_seen = (refused, blocked)
        return due and self.cfg.get("motion", "full") != "off"

    def start(self, words, **extra):
        self.bell_seen, self.scrub, self.tour = (0, False), None, None  # a new run's state counts from nothing
        self.state, self.selected, self.expanded, self.follow, self.message = State(), None, set(), True, ""
        self.motion = Motion()
        if self.next_root:  # T-1330: a run started from a project's view runs in that project
            (self.root, code), self.next_root = self.next_root, None
            self.history_dir = code or self.history_dir
        try:
            self.child = self.spawn(words, dict(self.opts, **extra))
        except OSError as e:  # a folder gone since, fm missing: say so, keep the screen
            self.child = None
            self.state.status, self.state.error = "error", f"couldn't start: {e}"
            self.state.note("error", text=f"couldn't start: {e}")

    def _spawn(self, words, opts):
        argv = [sys.executable, FM, "code", *words, "--json"]
        for k, v in opts.items():
            if v not in (None, False, ""):
                argv += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
        p = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True,
                             env=dict(os.environ, FOREMAN_CODE_ATTENDED="1"))  # T-1401: watched, not unattended
        threading.Thread(target=self._read, args=(p,), daemon=True).start()
        return p

    def _read(self, p):
        for line in p.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                continue
        err = p.stderr.read()
        p.wait()
        self.events.put({"event": "exit", "code": p.returncode, "stderr": err[-2000:]})

    def feed(self, e):
        try:
            self.state.apply(e)
        except ValueError as x:
            self.state.status, self.state.error = "error", str(x)
            self.state.note("error", text=str(x))
        if self.queue_run is not None and isinstance(e, dict) and e.get("event") == "exit":
            self._queue_after()  # T-1779
        elif self.tickets and isinstance(e, dict) and e.get("event") == "exit":  # T-1350: the rail's next
            if self.state.status == "closed":
                text, left = self.tickets[0], len(self.tickets) - 1
                self.tickets.pop(0)
                self.start([text])
                self.state.note("note", text=f"ticket: {text}" + (f" ({left} more on the rail)" if left else ""))
            else:
                self.state.note("note", text=f"tickets wait: this run is {self.state.status or 'stopped'}; "
                                             f"{len(self.tickets)} on the rail (T lists them)")

    # -- the queue (T-1779)
    QUEUE_BLOCKS = 2  # blocked tasks in a row that stop it: the queue isn't working, not one task

    def show_tickets(self):
        """T-1350: the rail as a list: space picks a ticket up and puts it down, arrows move it, Delete drops it."""
        self.view_back, self.mode, self.item_index, self.ticket_held = self.mode, "tickets", 0, False
        self._ticket_items()

    def _ticket_items(self):
        self.items = [(i, t, self.ticket_held and i == self.item_index) for i, t in enumerate(self.tickets)]

    def _tickets_key(self, k):
        cmd, i, n = self._list_cmd(k), self.item_index, len(self.tickets)
        if k in ("\x1b", "q") or cmd == "tickets":
            self.mode, self.ticket_held = "main", False
        elif k == " " and n:
            self.ticket_held = not self.ticket_held
        elif cmd in ("up", "down") and n:
            j = max(0, min(n - 1, i + (1 if cmd == "down" else -1)))
            if self.ticket_held and j != i:
                self.tickets[i], self.tickets[j] = self.tickets[j], self.tickets[i]
            self.item_index = j
        elif k in ("KEY_DC", "\x7f", "KEY_BACKSPACE") and n:
            self.tickets.pop(i)
            self.item_index, self.ticket_held = max(0, min(i, len(self.tickets) - 1)), False
        self._ticket_items()
        return None

    def work_queue(self):
        """T-1779: g works the project's queue here, one fm code --task run after another, until it is empty, two
        tasks in a row block, or s stops it; then a digest of the stretch lands in Activity."""
        self.queue_run = {"closed": [], "blocked": [], "streak": 0, "since": time.monotonic(), "task": None}
        if self.running():  # T-1783: the run going is left alone; its end counts and starts the queue
            self.queue_run["task"] = self.state.task
            self.message = "the queue starts when this run ends (s stops both)"
            return
        self._queue_next()

    def _queued(self):
        """The next task the project's queue holds (fm queue's order), or None."""
        try:
            p = subprocess.run([sys.executable, FM, "queue", "--json"], cwd=self.root, capture_output=True, text=True,
                               timeout=30)
            order = json.loads(p.stdout).get("order") if p.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            return None
        return next((str(b["id"]) for b in order or [] if isinstance(b, dict) and b.get("id")), None)

    def _queue_next(self):
        q = self.queue_run
        tid = self.queued()
        if tid is None or tid == q["task"]:  # empty, or the one that just ran is still first: it made no move
            return self._queue_end("the queue is empty" if tid is None else f"{tid} is still first after its run")
        q["task"] = tid
        self.start([], task=tid)
        self.state.note("note", text=f"working the queue: {tid} ({len(q['closed'])} closed so far; s stops it)")

    def _queue_after(self):
        q, st = self.queue_run, self.state
        tid = q["task"] or st.task
        if st.status == "closed":
            q["closed"].append(tid)
            q["streak"] = 0
        elif st.status == "blocked":
            q["blocked"].append(f"{tid}: {_printable_line(st.error, 120) or 'blocked'}")
            q["streak"] += 1
            if q["streak"] >= self.QUEUE_BLOCKS:
                return self._queue_end(f"{q['streak']} tasks in a row blocked")
        else:  # stopped (s), an error, a crash: the queue stops with it
            return self._queue_end(f"{tid} {st.status or 'ended'}")
        self._queue_next()

    def _queue_end(self, why):
        q, self.queue_run = self.queue_run, None
        mins = max(1, round((time.monotonic() - q["since"]) / 60))
        away = time.monotonic() - self.last_key >= 300
        head = "While you were away" if away else "The queue run"
        text = (f"{head} ({mins} min): {len(q['closed'])} closed" + (f" ({', '.join(q['closed'])})" if q["closed"] else "")
                + f", {len(q['blocked'])} blocked; stopped: {why}")
        self.state.note("note", text=text)
        for b in q["blocked"]:
            self.state.note("note", text=f"  blocked {b}")
        self.message = text

    def poll(self):
        """Take the events that arrived, and put back a previewed look not kept in time; whether any did (so the
        driver redraws)."""
        got = self._settings_expire()
        got = self._watch_tui() or got
        if self.tour:  # T-1309: the tour's next event, on its clock
            got = self._tour_tick() or got
        if self.runner_done:  # T-1511: a runner test's result came in
            got, self.runner_done = True, False
        if self.mode == "breakout" and not checks_running(self.state):  # T-1556: the checks stopped, so does the game
            got, self.mode = True, "main"
            self.message = f"the checks stopped: Breakout closed at {self.game.score} brick(s)"
        while True:
            try:
                self.feed(self.events.get_nowait())
            except queue.Empty:
                return got
            got = True


def _json_line(raw):
    try:
        e = json.loads(raw)
    except ValueError:
        return None
    return e if isinstance(e, dict) else None


def _alive(pid):
    """A pid that is a live process (a zombie isn't)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in "ZX"
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
            return not os.path.isdir("/proc")
        except OSError:
            return False


def history(folder):
    """Past runs, newest first: [{path, task, request, status, ts}] from code/<task>.events.jsonl, each read at its
    first line and its last 16 KB, not whole."""
    out = []
    try:
        names = [n for n in os.listdir(folder or "") if n.endswith(".events.jsonl")]
    except OSError:
        return out
    for n in names:
        path = os.path.join(folder, n)
        try:
            with open(path, "rb") as f:
                first = _json_line(f.readline())
                f.seek(max(0, os.fstat(f.fileno()).st_size - 16384))
                tail = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        last = next((e for e in map(_json_line, reversed(tail)) if e and e.get("event") in ("closed", "blocked", "error",
                                                                                            "undone")),
                    None)
        if first:
            out.append({"path": path, "task": n.split(".")[0], "request": str(first.get("request") or ""),
                        "status": (last or {}).get("event", "stopped"), "ts": first.get("ts") or os.path.getmtime(path)})
    return sorted(out, key=lambda x: -(x["ts"] or 0))


# ---------------------------------------------------------------- extensions: Python files in Foreman's home folder

HANDLERS = {}      # an fm code event kind, or "*" → [fn(state, event)]
EXT_COMMANDS = {}  # a command's name → fn(app)
EXT_LABELS = []    # (command, what it does) for the keys view
EXT_SETTINGS = {}  # T-1657: an extension's name → {key: its fmsettings schema entry}
EXT_LOADED = set()  # the folders load_extensions has run (fm settings loads them once to list their settings)
EXT_TYPES = ("bool", "int", "number", "string", "choice")
# T-1315: an extension's time budgets, in seconds; past one it's switched off for the session with a note in Activity.
PANEL_BUDGET, PANEL_STRIKES = 0.05, 3  # a panel over 50 ms on 3 frames running
HANDLER_BUDGET = 0.05                  # an event handler over 50 ms once
COMMAND_BUDGET = 2.0                   # a command over 2 s once


def _bare(s):
    """A name TOML writes bare and a dotted key splits back: letters, digits, _ and -, 40 at most."""
    return isinstance(s, str) and 0 < len(s) <= 40 and all(c.isascii() and (c.isalnum() or c in "_-") for c in s)


class API:
    """What an extension's setup(api) gets: four calls, each adding to a registry the TUI reads. `name` is the
    extension's file name: its settings' group. Version 2 (T-1657) adds setting."""
    version = 2

    def __init__(self, name=None):
        self.name = name

    def setting(self, key, type, default, help, choices=None, range=None):
        """T-1657: a setting of this extension, in tui.toml as [ext.NAME] key = value; type bool, int, number, string or
        choice (with choices); range (low, high; None for no bound) for a number. fm settings and the settings screen
        list it under the extension's group and check and set it as they do the app's own; the extension reads
        app.cfg["ext"][NAME][key]. ValueError for one that can't be declared (said as the extension's error)."""
        from sampo import settings
        if not _bare(self.name):
            raise ValueError(f"extension {self.name!r}: settings need a file name of letters, digits, _ and -")
        if not _bare(key):
            raise ValueError(f"setting {key!r}: a name of letters, digits, _ and -, 40 at most")
        if type not in EXT_TYPES:
            raise ValueError(f"setting {key}: type {type!r}: one of {', '.join(EXT_TYPES)}")
        if (type == "choice") != bool(choices) or (choices and not all(isinstance(c, str) and c and c.isprintable()
                                                                       for c in choices)):
            raise ValueError(f"setting {key}: choices, a list of words, for a choice (and only for one)")
        if range is not None and (type not in ("int", "number") or len(range) != 2 or not all(
                x is None or isinstance(x, (int, float)) and not isinstance(x, bool) for x in range)):
            raise ValueError(f"setting {key}: range, (low, high) with None for no bound, for a number (and only one)")
        e = settings._entry("app", f"ext.{self.name}.{key}", type, default, _escaped(str(help))[:200],
                              f"ext.{self.name}", "next run", choices=choices, range=tuple(range) if range else None)
        if settings.ext_value(e, default) != default or isinstance(default, bool) != (type == "bool"):
            raise ValueError(f"setting {key}: default {default!r} isn't a {type}")  # "5" for an int: not its own
        EXT_SETTINGS.setdefault(self.name, {})[key] = e
        DEFAULTS["ext"].setdefault(self.name, {})[key] = default

    def panel(self, name, title, fn):
        """A panel a layout can name: fn(state, width, height, app) → lines, each a list of (text, style)."""
        PANELS[name] = (title, fn)

    def command(self, name, fn, key=None, label=None):
        """A command a key can run: fn(app). key binds it unless that key is taken; label shows in the keys view."""
        EXT_COMMANDS[name] = fn
        if name not in COMMANDS:
            COMMANDS.append(name)
        if label:
            EXT_LABELS.append((name, label))
        for k in (parse_key(key) or []) if key else []:
            DEFAULTS["keys"].setdefault(k, name)

    def on(self, event, fn):
        """fn(state, event) after the state takes each event of this kind ("*": every event)."""
        HANDLERS.setdefault(event, []).append(fn)


def extensions_dir():
    from sampo import core
    return os.path.join(core.foreman_home(), "tui")


class _Late(BaseException):
    """An extension's load past its budget (T-1657)."""


def load_extensions(folder=None, budget=None):
    """Run setup(API()) of each *.py in Smith's home folder's tui/ (a project's files never: they'd run as you).
    [one line per file that failed or was skipped]; the others still load. T-1657: whatever one raises, SystemExit
    too, is its line; `budget`, seconds a file may take to load (SIGALRM: the main thread only), or it's skipped."""
    import importlib.util
    folder = folder or extensions_dir()
    EXT_LOADED.add(folder)
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".py"))
    except OSError:
        return []
    errors, uid = [], getattr(os, "getuid", lambda: None)()
    for n in names:
        path = os.path.join(folder, n)
        try:
            st = os.stat(path)
            if (uid is not None and st.st_uid != uid) or st.st_mode & 0o022:
                errors.append(f"{path}: skipped: another user can change it")
                continue
            with _timebox(budget):
                spec = importlib.util.spec_from_file_location(f"fm_tui_ext_{n[:-3]}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.setup(API(n[:-3]))
        except _Late:
            errors.append(f"{path}: took over {budget} s to load: skipped")
        except BaseException as e:  # its own bug, a sys.exit too: one line, and the next extension still loads
            errors.append(f"{path}: {type(e).__name__}: {e}")
    return errors


@contextlib.contextmanager
def _timebox(seconds):
    """_Late raised in the block past `seconds` (None, or off the main thread: no limit)."""
    if not seconds or threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        yield
        return

    def late(*_):
        raise _Late()
    old = signal.signal(signal.SIGALRM, late)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


# ---------------------------------------------------------------- config: tui.toml in Foreman's home folder

COMMANDS = [c for c, _ in COMMAND_LABELS] + ["page_up", "page_down"]  # what a key can run (extensions add theirs)
KEY_ALIASES = {"tab": ["\t"], "enter": ["\n", "KEY_ENTER"], "esc": ["\x1b"], "space": [" "], "up": ["KEY_UP"],
               "down": ["KEY_DOWN"], "left": ["KEY_LEFT"], "right": ["KEY_RIGHT"], "pgup": ["KEY_PPAGE"],
               "pgdn": ["KEY_NPAGE"], "home": ["KEY_HOME"], "end": ["KEY_END"], "backspace": ["KEY_BACKSPACE"]}
STYLE_FIELDS = ("fg", "bg", "bold", "italic", "reverse", "paint")


def config_path():
    from sampo import core
    return os.path.join(core.foreman_home(), "tui.toml")


def parse_key(name):
    """A key's name in tui.toml as the keys curses reports: [key, …], or None for a name that isn't a key."""
    low = str(name).lower()
    if low in KEY_ALIASES:
        return KEY_ALIASES[low]
    if len(low) == 6 and low.startswith("ctrl+") and low[5].isalpha():
        return ["^" + low[5].upper()]
    if low[:1] == "f" and low[1:].isdigit() and 1 <= int(low[1:]) <= 12:
        return [f"KEY_F({int(low[1:])})"]
    return [name] if len(name) == 1 and name.isprintable() and name != " " else None


def key_name(k):
    """The reverse: how tui.toml names a key curses reports."""
    alias = next((n for n, ks in KEY_ALIASES.items() if k in ks), None)
    if alias:
        return alias
    if len(k) == 2 and k[0] == "^" and k[1].isalpha():
        return "ctrl+" + k[1].lower()
    return f"f{k[6:-1]}" if k.startswith("KEY_F(") else k


def _colour(v):
    if isinstance(v, str):
        return len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255


def contrast(a, b):
    """WCAG contrast ratio of two colours (1 to 21): fmdesign's formula on the colours' sRGB (T-1675)."""
    from sampo import design  # lazy: only a [colors] check needs it
    return design.contrast(*(tuple(v / 255 for v in _rgb(c)) for c in (a, b)))


def _faint(spec, theme):
    """Why a style's text can't be read on what it sits on (its own background or the screen's), or None."""
    fg, bg = spec.get("fg"), spec.get("bg") or theme.get("screen", {}).get("bg")
    if fg is None or bg is None:
        return None
    ratio = contrast(fg, bg)
    return f"{fg} on {bg} is too faint to read ({ratio:.1f}:1; text needs 3:1)" if ratio < 3 else None


def _style_error(name, spec, theme):
    if name not in theme:
        return f"no such style ({', '.join(theme)})"
    if not isinstance(spec, dict):
        return None if _colour(spec) else f"{spec!r} isn't a colour (#rrggbb or 0-255)"
    for k, v in spec.items():
        if k not in STYLE_FIELDS:
            from sampo import core
            return f"{k}: not a style field ({core.joinmore(STYLE_FIELDS, 5)})"
        if k in ("fg", "bg") and not _colour(v):
            return f"{k} = {v!r} isn't a colour (#rrggbb or 0-255)"
        if k not in ("fg", "bg") and not isinstance(v, bool):
            return f"{k} is true or false"
    return None


def _layout_error(k, v, table, cfg):
    names = lambda xs: isinstance(xs, list) and all(isinstance(n, str) for n in xs)
    size = lambda x: x == "auto" or isinstance(x, int) and not isinstance(x, bool) and x > 0
    if k in ("top", "narrow", "columns"):
        cols = v if k == "columns" else [v]
        if not (isinstance(cols, list) and cols and all(names(c) and (c or k != "columns") for c in cols)):
            return "a list of columns, each a list of panel names" if k == "columns" else "a list of panel names"
        unknown = [n for c in cols for n in c if n not in PANELS]
        return f"no panel {unknown[0]!r} (panels: {', '.join(PANELS)})" if unknown else None
    if k == "widths":
        cols = table.get("columns", cfg["layout"]["columns"])
        cols = cols if isinstance(cols, list) else cfg["layout"]["columns"]  # T-1657: a bad one is skipped, said
        ok = isinstance(v, list) and len(v) == len(cols) and all(size(x) and x != "auto" for x in v)
        return None if ok else "one whole number above 0 per column"
    if k == "heights":
        ok = isinstance(v, list) and all(isinstance(c, list) and all(size(x) for x in c) for c in v)
        return None if ok else 'a list per column of "auto" or a whole number above 0 per panel'
    if k == "narrow_heights":
        return None if isinstance(v, list) and all(size(x) for x in v) else '"auto" or a whole number above 0 per panel'
    if k == "narrow_below":
        return None if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else "a number of columns"
    return f"not a layout setting ({', '.join(DEFAULTS['layout'])})"


def _escaped(s):
    """s with each control or format character (a terminal escape, a bell, a bidi override) written as its escape."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in s)


def load_config(path=None, env=None, profile=True):
    """DEFAULTS with the user's tui.toml over them: (config, [one line per bad setting]). A bad setting is skipped, the
    rest apply. T-1656: then the first [profile.NAME] whose when clause the terminal meets (fmsettings.facts of `env`,
    the environment by default), its settings through the same checks; cfg["profile"] = {name, sets: what it set}.
    profile=False: the file's own settings alone (--print-config prints the profiles as tables). T-1657: its [[rule]]
    tables, checked, as cfg["rules"] (rule_list); [ext.NAME] tables by the settings each extension declared."""
    import copy
    import tomllib
    from sampo import settings
    cfg = copy.deepcopy(DEFAULTS)
    path = path or config_path()
    try:
        data, st = settings._read_raw(path)  # T-1543: never a FIFO (it would block), a device or a file past 1 MB
        if st is None:
            return cfg, []
        user = tomllib.loads(data.decode("utf-8"))
    except settings.SettingError as e:  # its message starts with the path
        return cfg, [_escaped(str(e))]
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError) as e:
        return cfg, [f"{path}: {_escaped((str(e).splitlines() or [type(e).__name__])[0])}"]
    errors = []
    bad = lambda msg: errors.append(f"{path}: {_escaped(msg)}")  # a name from the file, said, never run by a terminal
    table, rules = user.pop("profile", None), user.pop("rule", None)
    _apply(cfg, user, bad)
    found = settings.profile_list(table, settings.facts(env)) if table is not None else []
    for p in found:
        if p["problem"]:
            bad(p["problem"])
    on = next((p for p in found if p["matches"]), None) if profile else None
    if on:
        at, own, mine = f"profile.{on['name']}.", [], on["settings"]
        _apply(cfg, mine, lambda msg: own.append(at + msg))
        for msg in own:
            bad(msg)
        # each key as the file spells it (a message names it so), kept by the key it means: a value skipped isn't set
        def spelled(t, pre=""):  # table.key, and T-1657's ext.NAME.key; a style's own table is its value
            for k, v in t.items():
                if isinstance(v, dict) and pre in ("", "ext."):
                    yield from spelled(v, f"{pre}{k}.")
                else:
                    yield f"{pre}{k}"
        hit = lambda k, m: m.startswith(f"{at}{k} =") or any(  # foo: not a setting, or a table above it
            m.startswith(f"{at}{'.'.join(k.split('.')[:i])}:") for i in range(1, k.count(".") + 2))
        cfg["profile"] = {"name": on["name"], "sets": list(dict.fromkeys(
            settings._norm(k) for k in spelled(mine) if not any(hit(k, m) for m in own)))}
    if rules is not None:  # T-1657: checked against the layout they switch from, a profile's included
        cfg["rules"], said = rule_list(rules, cfg)
        for msg in said:
            bad(msg)
    return cfg, errors


SCOPES = ("everywhere", "project", "run")  # T-1611: where the settings screen's :set goes (s cycles it)
SCOPE_WORDS = {"everywhere": "everywhere (tui.toml)", "project": "this project", "run": "this run only"}
PIN_FREE = ("keys", "motion")  # T-1611: the person's own (their hands, their comfort): a repo's policy never pins them
LAYER_ONLY = ("profile", "rule", "ext")  # T-1611: tables only tui.toml has; a project's file or a pin sets settings


def project_settings_path(root):
    """T-1611: this project's own settings, beside tui.toml and protected by the guard as it is (they bind keys),
    never in the repo (a clone brings none)."""
    from sampo import core
    return os.path.join(core.foreman_home(), "tui.projects", core.slug_for(os.path.realpath(root)) + ".toml")


def policy_path(root):
    """T-1611: the repo's policy, committed with it: [pin] settings no narrower scope overrides."""
    from sampo import core as c
    paths = [os.path.join(root, m,"policy.toml") for m in c.MIRRORS]
    return next((x for x in paths if os.path.isfile(x)), paths[0])


def _settings_file(path):
    """A settings file read the way tui.toml is (bounded, never a FIFO or a device): (its tables, a problem or None);
    ({}, None) when there is none."""
    import tomllib
    from sampo import settings
    try:
        data, st = settings._read_raw(path)
        return (tomllib.loads(data.decode("utf-8")) if st is not None else {}), None
    except settings.SettingError as e:
        return {}, _escaped(str(e))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError) as e:
        return {}, f"{path}: {_escaped((str(e).splitlines() or [type(e).__name__])[0])}"


def flat_keys(table):
    """The settings a parsed settings table sets, by name: theme, colors.ok, layout.columns, keys.q, …"""
    from sampo import settings
    out = []
    for k, v in table.items():
        out += [settings._norm(f"{k}.{n}") for n in v] if isinstance(v, dict) else [k]
    return out


def layered(cfg, root, run=None):
    """T-1611: `cfg` (tui.toml's: everywhere) with the narrower scopes over it, in order: this project's settings
    (project_settings_path), this run's (`run`, held in memory), then the repo's policy pins ([pin] in policy_path),
    which no scope under them overrides; keys and motion are never pinned. Each setting checked as tui.toml's are, a
    bad one said and skipped. (config, [problems]); config["scopes"] = {setting: the narrowest scope that set it}."""
    import copy
    cfg, problems, where = copy.deepcopy(cfg), [], {}
    project, why = _settings_file(project_settings_path(root))
    policy, why2 = _settings_file(policy_path(root))
    problems += [w for w in (why, why2) if w]
    pins = policy.get("pin", {})
    if not isinstance(pins, dict):
        problems.append(f"{policy_path(root)}: pin: a [pin] table")
        pins = {}
    problems += [f"{policy_path(root)}: pin.{k}: never pinned, it stays the person's own" for k in pins if k in PIN_FREE]
    for name, table, path in (("project", project, project_settings_path(root)),
                              ("run", copy.deepcopy(run or {}), "this run"),
                              ("pin", {k: v for k, v in pins.items() if k not in PIN_FREE}, policy_path(root))):
        problems += [f"{path}: {k}: only tui.toml has [{k}] tables" for k in LAYER_ONLY if table.pop(k, None)
                     is not None]
        said = []
        _apply(cfg, table, said.append)
        problems += [f"{path}: {_escaped(m)}" for m in said]
        where.update(dict.fromkeys(flat_keys(table), name))
    cfg["scopes"] = where
    return cfg, problems


# T-1657: what a [[rule]]'s when clause can name: the run's events, each judged from the State the app already has
RULE_EVENTS = {
    "running": (lambda st: st.status in LIVE, "a run going"),
    "verify": (checks_running, "the checks running: from the first result to the run's end"),
    "failing": (lambda st: st.status in ("blocked", "error", "stopped") or st.status != "closed" and any(
        c["state"] == "fail" and not c.get("first") for c in st.criteria), "a check failing, or the run stopped"),
    "closed": (lambda st: st.status == "closed", "the run closed"),
}
RULE_KEYS, RULES_MAX = ("when", "layout", "panels"), 16


def rule_list(table, cfg):
    """tui.toml's [[rule]] tables (`table`, as parsed), checked against `cfg`: ([{when, layout, panels}], [problems]).
    A rule whose when clause is missing, empty or names an event the app doesn't know (RULE_EVENTS) is passed over; a
    key it doesn't know, a layout setting that's wrong or a panel that isn't one is skipped, the rest of it kept."""
    if not isinstance(table, list):
        return [], ["rule: a [[rule]] table per rule: passed over"]
    out, said, names = [], [], ", ".join(RULE_EVENTS)
    if len(table) > RULES_MAX:  # each is drawn when the app starts and judged each frame
        said.append(f"rule.{RULES_MAX + 1} on: {RULES_MAX} rules at most: passed over")
    for i, r in enumerate(table[:RULES_MAX], 1):
        at, when = f"rule.{i}", r.get("when") if isinstance(r, dict) else None
        if not isinstance(r, dict):
            why = f"{at}: a table with a when clause"
        elif not isinstance(when, dict) or not when:
            why = f"{at}.when: a table of events, at least one ({names})"
        else:
            why = next((f"{at}.when.{k}" + (f" = {repr(v)[:60]}: true or false" if k in RULE_EVENTS else
                                            f": not an event ({names})")
                        for k, v in when.items() if k not in RULE_EVENTS or not isinstance(v, bool)), None)
        if why:
            said.append(f"{why}: passed over")
            continue
        rule = {"when": when, "layout": {}, "panels": {}}
        for k, v in r.items():
            if k == "when":
                continue
            if k not in RULE_KEYS:
                said.append(f"{at}.{k}: not a rule setting ({', '.join(RULE_KEYS)})")
            elif not isinstance(v, dict):
                said.append(f"{at}.{k}: a table")
            elif k == "layout":
                for n, x in v.items():
                    msg = _layout_error(n, x, v, cfg)
                    if msg:
                        said.append(f"{at}.layout.{n}: {msg}")
                    else:
                        rule["layout"][n] = x
            else:
                for a, b in v.items():
                    if a in PANELS and isinstance(b, str) and b in PANELS:
                        rule["panels"][a] = b
                    else:
                        said.append(f"{at}.panels.{a}: a panel shown in another's place ({', '.join(PANELS)})")
        out.append(rule)
    return out, said


def switched(lay, rule):
    """The layout `lay` as `rule` switches it: its layout settings over lay's, then each panel it swaps."""
    lay = dict(lay, **rule["layout"])
    swap = rule["panels"]
    if swap:
        one = lambda names: [swap.get(n, n) for n in names]
        lay.update(top=one(lay.get("top") or []), narrow=one(lay.get("narrow") or ["log"]),
                   columns=[one(c) for c in lay.get("columns") or [["log"]]])
    return lay


def rule_layout(cfg, st, app=None):
    """The layout to draw for the run `st`: the first rule whose events all hold switches cfg's own, which is back the
    moment they don't (judged each frame from the state: nothing to put back). app.rule: that rule's number, or None
    (the status line shows it)."""
    lay, on = cfg.get("layout") or DEFAULTS["layout"], None
    try:
        for i, r in enumerate(cfg.get("rules") or (), 1):
            if all(RULE_EVENTS[k][0](st) == v for k, v in r["when"].items()):
                lay, on = switched(lay, r), i
                break
    except Exception as e:  # a state it can't judge: the file's own layout, said once, never a crashed screen
        if app is not None and not app.rule_said:
            app.rule_said = True
            app.state.note("error", text=f"rules: {type(e).__name__}: {e}; the file's own layout shows")
    if app is not None:
        app.rule = on
    return lay


def _apply(cfg, user, bad):
    """`user`'s settings (a parsed tui.toml, or a profile's) over `cfg`, each checked: a bad one said through bad(msg)
    and skipped."""
    from sampo import settings
    if "theme" in user:
        if isinstance(user["theme"], str) and user["theme"] in PRESETS:  # a list or table is unhashable: a bad value
            cfg["preset"], cfg["theme"] = user["theme"], PRESETS[user["theme"]]()
        else:
            bad(f"theme = {user['theme']!r}: no such theme ({', '.join(PRESETS)})")
    if "palette" in user:  # before [colors], which changes single styles over it
        val = settings.canon("palette", user["palette"])
        if val:
            cfg["palette"], cfg["theme"] = val, preset_theme(cfg["preset"], val)
        else:
            bad(f"palette = {user['palette']!r}: one of {', '.join(settings.SHARED['palette']['choices'])}")
    for key, val in user.items():
        if key in ("theme", "palette"):
            continue
        if key == "motion":
            if settings.canon("motion", val):  # T-1496: the mod's old "calm" is reduced
                cfg["motion"] = settings.canon("motion", val)
            else:
                bad(f"motion = {val!r}: one of {', '.join(MOTIONS)}")
        elif key == "live_theme":  # T-3055: the live page's default look; a viewer's own pick wins
            if val in LIVE_THEMES:
                cfg["live_theme"] = val
            else:
                bad(f"live_theme = {val!r}: one of {', '.join(LIVE_THEMES)}")
        elif key == "border":
            if isinstance(val, str) and val in BORDERS:
                cfg["border"] = val
            else:
                bad(f"border = {val!r}: one of {', '.join(BORDERS)}")
        elif key in ("colors", "layout", "keys", "ext") and not isinstance(val, dict):
            bad(f"{key}: a [{key}] table")
        elif key == "ext":  # T-1657: an extension's settings, each checked by the schema it declared
            for name, table in val.items():
                mine = EXT_SETTINGS.get(name)
                if mine is None:
                    bad(f"ext.{name}: no extension declares settings by that name ({', '.join(EXT_SETTINGS) or 'none'})")
                elif not isinstance(table, dict):
                    bad(f"ext.{name}: an [ext.{name}] table")
                else:
                    for k, v in table.items():
                        try:
                            if k not in mine:
                                raise settings.SettingError(f"ext.{name}.{k}: not a setting of {name} "
                                                              f"({', '.join(mine)})")
                            cfg["ext"].setdefault(name, {})[k] = settings.ext_value(mine[k], v)
                        except settings.SettingError as x:
                            bad(str(x))
        elif key == "colors":
            was = {k: dict(cfg["theme"].get(k) or {}) for k in ("accent", "ok", "hot", "steel")}
            for name, spec in val.items():
                msg = _style_error(name, spec, cfg["theme"])
                if not msg and name not in ("screen", "panel", "border", "track", "shade"):  # text has to be read
                    msg = _faint(spec if isinstance(spec, dict) else {"fg": spec}, cfg["theme"])
                if msg:
                    bad(f"colors.{name}: {msg}")
                else:
                    cfg["theme"][name] = spec if isinstance(spec, dict) else {"fg": spec}
            # T-2876: where hot and steel only stood in for accent and ok (a palette without heat), a saved accent or ok
            # recolours running and finished marks too, as before; the forge's own hot and steel stay its own
            th = cfg["theme"]
            for src, dst in (("accent", "hot"), ("ok", "steel")):
                if src in val and dst not in val and th.get(src, {}).get("fg") and \
                        was[dst].get("fg") == was[src].get("fg"):
                    th[dst] = dict(th.get(dst) or {}, fg=th[src]["fg"])
                    if dst == "steel" and "chip_steel" not in val:
                        th["chip_steel"] = dict(th.get("chip_steel") or {}, bg=th[src]["fg"])
        elif key == "layout":
            for k, v in val.items():
                msg = _layout_error(k, v, val, cfg)
                if msg:
                    bad(f"layout.{k}: {msg}")
                else:
                    cfg["layout"][k] = v
        elif key == "keys":
            for name, cmd in val.items():
                keys = parse_key(name)
                if not keys:
                    bad(f"keys.{name}: not a key (a letter, ctrl+x, f1-f12, {', '.join(KEY_ALIASES)})")
                elif cmd != "none" and cmd not in COMMANDS:
                    bad(f"keys.{name} = {cmd!r}: no such command ({', '.join(COMMANDS)}, or none)")
                else:
                    for k in keys:
                        cfg["keys"].pop(k, None) if cmd == "none" else cfg["keys"].__setitem__(k, cmd)
        else:
            bad(f"{key}: not a setting (theme, motion, palette, border, colors, layout, keys)")


def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(map(_toml, v)) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml(x)}" for k, x in v.items()) + " }" if v else "{}"
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a TOML basic string


def _toml_key(k):
    return k if k and all(c.isascii() and (c.isalnum() or c in "_-") for c in k) else json.dumps(k, ensure_ascii=False)


def print_config(cfg, path=None, profiles=()):
    """The config as tui.toml text that loads back the same: a starting point to edit. T-1656: `profiles`
    (fmsettings.profile_list) follow as [profile.NAME] tables, as written."""
    out = [f"# sampo smith tui's settings{f': {path}' if path else ''}. Delete what you don't change.",
           f"theme = {_toml(cfg.get('preset', 'night'))}  # {', '.join(PRESETS)}; [colors] changes single styles",
           f"motion = {_toml(cfg.get('motion', 'full'))}  # full; reduced: colours change, nothing moves; off",
           f"palette = {_toml(cfg.get('palette', 'default'))}  # default; cb: pass blue, fail orange; riso: "
           "risograph inks",
           f"border = {_toml(cfg['border'])}  # {', '.join(BORDERS)}", "",
           "[colors]  # a style: a colour (#rrggbb or 0-255), or { fg, bg, bold, italic, reverse }"]
    out += [f"{_toml_key(n)} = {_toml(spec)}" for n, spec in cfg["theme"].items()]
    out += ["", f"[layout]  # panels: {', '.join(PANELS)}"] + [f"{_toml_key(k)} = {_toml(v)}" for k, v in cfg["layout"].items()]
    out += ["", f"[keys]  # a letter, ctrl+x, f1-f12, {', '.join(KEY_ALIASES)}; \"none\" unbinds one"]
    seen = set()
    for k, cmd in cfg["keys"].items():
        if key_name(k) not in seen:
            seen.add(key_name(k))
            out.append(f"{_toml_key(key_name(k))} = {_toml(cmd)}")
    for p in profiles:
        if p["settings"] is None:  # not a table: nothing to print
            continue
        head, mine = f"profile.{_toml_key(p['name'])}", p["settings"]
        out += ["", f"[{head}]", f"when = {_toml(p['when'])}"]
        out += [f"{_toml_key(k)} = {_toml(v)}" for k, v in mine.items() if not isinstance(v, dict)]
        for k, v in mine.items():
            if isinstance(v, dict):
                out += [f"[{head}.{_toml_key(k)}]"] + [f"{_toml_key(n)} = {_toml(x)}" for n, x in v.items()]
    for name, mine in (cfg.get("ext") or {}).items():  # T-1657: names _bare keeps bare
        out += ["", f"[ext.{name}]"] + [f"{_toml_key(k)} = {_toml(v)}" for k, v in mine.items()]
    for r in cfg.get("rules") or ():
        out += ["", "[[rule]]", f"when = {_toml(r['when'])}"] + [f"{k} = {_toml(r[k])}" for k in RULE_KEYS[1:] if r[k]]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- drivers

def replay_file(path, cfg, plain, out=sys.stdout):
    """fm code tui --events FILE: the run on the screen, or (plain) its Activity lines; 0 for a closed run, 1 for
    another, 2 when FILE can't be read."""
    if not os.path.isfile(path):
        print(f"sampo smith tui: can't read {path}: no such file (sampo smith … --json > FILE records a run)", file=sys.stderr)
        return 2
    app = App(cfg, spawn=lambda *a, **k: None)
    app.replay(path)
    if app.message.startswith("can't read"):
        print(f"sampo smith tui: {app.message}", file=sys.stderr)
        return 2
    if not plain:
        run_screen(app)
        return 0
    for entry in app.state.log:
        print(__import__("sampo.core").core.redact(entry_text(entry)), file=out, flush=True)  # T-1302: piped lines get shared
    return 0 if app.state.status == "closed" else 1


def run_plain(app, out=sys.stdout):
    """--plain: each new Activity line as text, the model's own words included; the run's exit status."""
    while True:
        e = app.events.get()
        seen = len(app.state.log)
        app.feed(e)
        for entry in app.state.log[seen:] if len(app.state.log) >= seen else app.state.log:
            print(entry_text(entry), file=out, flush=True)
        if e.get("event") == "exit":
            return 0 if app.state.status == "closed" else 1


def key_of(k):
    """A key as tui.toml names it: a control key as ^R, Enter's \\r as \\n (parse_keys does this for raw input)."""
    if k == "\r":
        return "\n"
    if len(k) == 1 and ord(k) < 32 and k not in "\t\n\x1b\b":
        return "^" + chr(ord(k) + 64)
    return k


def _loop(io, app):
    """Draw, wait for a key or the next frame, repeat: as often as what's on screen moves (App.fps)."""
    term, w, h = Term(colour_depth()), 80, 24
    app.glyphs = io.widths(PROBE) if hasattr(io, "widths") else {}
    if term.depth == 0:  # NO_COLOR: the mono theme, whose chips are reversed and panels untinted
        app.cfg, app.no_colour = dict(app.cfg, theme=_mono(), preset="mono"), True  # kept when settings reload
    shown = None
    io.write("\x1b[22;0t")  # T-1310: keep the terminal's own title, to put back after
    try:
        while not app.done:
            app.poll()
            if io.resized():
                w, h = io.size()
                term.prev = None
                io.write("\x1b[2J")
            t, app.size = app.clock(), (w, h)
            frame = term.frame(render(app.state, w, h, app.cfg, app, t=t), app.cfg)
            title = title_text(app.state)
            if title != shown:  # T-1310: the run's state in the title, so another window shows it
                frame, shown = frame + f"\x1b]0;{title}\x07", title
            io.write(frame + ("\a" if app.bell_due() else ""))
            for k in io.keys(1 / app.fps(t)):
                app.key(k)
            if app.handoff and hasattr(io, "outside"):  # T-1341: the terminal to a live CLI, then back
                io.outside(app.run_handoff)
    finally:
        io.write("\x1b[23;0t")


def run_screen(app, wrapper=None):
    """The screen until q. Whatever ends it (q, Ctrl-C, a crash, a closed terminal) stops the run first: fm code
    runs in its own session and would carry on into a closed pipe. wrapper owns the terminal (fmcodeterm.session)."""
    def hangup(*_):
        raise SystemExit(129)
    old = {}
    for name in ("SIGHUP", "SIGTERM"):  # a closed terminal or a kill: the run stops and the terminal comes back
        try:
            old[name] = signal.signal(getattr(signal, name), hangup)
        except (ValueError, AttributeError):  # not the main thread, or no such signal here
            pass
    try:
        (wrapper or session)(lambda io: _loop(io, app))
        sys.stdout.write(final_frame(app))  # T-1305: the screen as it was, in the scrollback, once the terminal is back
        if app.tickets:  # T-1350: tickets are this session's; the ones left are said, not lost silently
            sys.stdout.write("tickets not run:\n" + "".join(f"  {i}. {t}\n" for i, t in enumerate(app.tickets, 1)))
        sys.stdout.flush()
    finally:
        app.stop()
        for name, handler in old.items():
            signal.signal(getattr(signal, name), handler)


def final_frame(app, size=None):
    """T-1305: the last screen as plain text (no colour, no control character, trailing spaces trimmed), for the
    scrollback after the alternate screen closes, so a run stays readable after q."""
    w, h = size or shutil.get_terminal_size((100, 30))
    try:
        text = render(app.state, w, h, app.cfg, app).text()
    except Exception:  # the scrollback copy never turns a clean quit into a crash
        return ""
    lines = ["".join(c if c.isprintable() else " " for c in line).rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def record(events, path, size=(120, 34), fps=20, seconds=None, cfg=None, hold=2.5):
    """A run's events played back as a video (mp4, or gif by its name): frames drawn at their instant, put to PNG by
    rsvg-convert and joined by ffmpeg. For the README, the site and a bug report."""
    if not (shutil.which("ffmpeg") and shutil.which("rsvg-convert")):
        raise RuntimeError("sampo smith tui --record needs ffmpeg and rsvg-convert on PATH")
    from sampo import core
    import tempfile
    cfg = cfg or DEFAULTS
    events = core.redact_obj([e for e in events if isinstance(e, dict)])  # T-1302: frames never hold a token
    seconds = seconds or max(4.0, min(16.0, len(events) * 0.45))
    gap, app = seconds / max(len(events), 1), App(cfg, spawn=lambda *a, **k: None)
    with tempfile.TemporaryDirectory(prefix="fm-tui-") as tmp:
        fed, svg = 0, os.path.join(tmp, "frame.svg")
        for f in range(int((seconds + hold) * fps)):
            t = f / fps
            while fed < len(events) and fed * gap <= t:
                app.feed(events[fed])
                fed += 1
            with open(svg, "w", encoding="utf-8") as fh:
                fh.write(to_svg(render(app.state, size[0], size[1], cfg, app, t=1000.0 + t), cfg))
            subprocess.run(["rsvg-convert", "-o", os.path.join(tmp, f"f{f:05d}.png"), svg], check=True)
        args = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", os.path.join(tmp, "f%05d.png")]
        args += ["-vf", "split[a][b];[a]palettegen[p];[b][p]paletteuse"] if path.endswith(".gif") else [
            "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        subprocess.run(args[:1] + ["-nostdin"] + args[1:] + [os.path.abspath(path)], check=True, stdin=subprocess.DEVNULL)
    return path


SAMPLE_RUN = [  # T-1311: a short run every theme draws in the gallery
    {"event": "start", "request": "fix add", "model": "sample", "schema": 1},
    {"event": "plan", "plan": {"type": "FIX", "tier": "S", "title": "Make calc.add add",
                               "criteria": [{"text": "calc.add adds", "verify": "python3 test_calc.py"}],
                               "steps": ["write the failing test", "fix calc.add"]}},
    {"event": "task", "text": "task T-0001 created and focused", "task": "T-0001"},
    {"event": "verify", "criterion": 1, "label": "before", "exit": 1, "cmd": "python3 test_calc.py",
     "output": "AssertionError: -1"},
    {"event": "step", "step": 1},
    {"event": "tokens", "call_in": 1200, "call_out": 80, "total_in": 1200, "total_out": 80},
    {"event": "action", "action": "read", "target": "calc.py", "result": "ok", "detail": "1 def add"},
    {"event": "action", "action": "run", "target": "rm -rf ~", "result": "refused", "detail": "the guard said no"},
    {"event": "step", "step": 2},
    {"event": "tokens", "call_in": 2100, "call_out": 140, "total_in": 3300, "total_out": 220},
    {"event": "action", "action": "edit", "target": "calc.py", "result": "ok", "detail": "edited"},
    {"event": "changed", "step": 2, "files": ["calc.py"]},
    {"event": "verify", "criterion": 1, "label": "verify", "exit": 0, "cmd": "python3 test_calc.py"},
]


# T-1309: fm code tui --tour: the sample run and its close, an event every TOUR_GAP seconds, the last held TOUR_HOLD;
# each caption shows from its event on, naming the panel that event lights
TOUR_GAP, TOUR_HOLD = 2.0, 3.0
TOUR_RUN = [dict(e, ts=1760000000.0 + 9 * i) for i, e in enumerate(SAMPLE_RUN + [  # timed, so Time charts it
    {"event": "closed", "text": "T-0001 closed; committed 4f2a9c1", "task": "T-0001", "tokens": [3300, 220]}])]
TOUR = {0: "sampo smith works one task to proof. The header names the request and the model; the bar below, the state.",
        1: "The track: a station per stage (the plan, each step, the checks, the close), lit as the run gets there.",
        3: "Checks: each criterion runs its own command. Red before the fix shows the test can fail.",
        4: "Activity: every action, under the step it serves. Enter opens a line, / searches them.",
        7: "The guard: rm -rf ~ is refused before it runs, and the run carries on.",
        9: "Tokens per call: what each model call read and wrote. Time charts the run's clock.",
        11: "Changed: the files each step touched. d opens the diff, step by step.",
        12: "Checks again: green after the fix. A run closes only on a passing check.",
        13: "Closed and committed. n starts your own run, ? lists every key, q leaves the tour."}


def tour_caption(fed):
    """The caption once `fed` of TOUR_RUN's events are on the screen."""
    shown = [k for k in TOUR if k < fed]
    return TOUR[max(shown)] if shown else None


def tour_plain(out=sys.stdout):
    """--tour --plain: each caption before the Activity lines its event adds."""
    app = App(DEFAULTS, spawn=lambda *a, **k: None)
    for i, e in enumerate(TOUR_RUN):
        seen = len(app.state.log)
        app.feed(e)
        if i in TOUR:
            print(f"▶ {TOUR[i]}", file=out)
        for entry in app.state.log[seen:]:
            print("  " + entry_text(entry), file=out)
    return 0


def tour_svg(path, size=(110, 32), cfg=None):
    """T-1309: the tour as one animated SVG: a frame per event, drawn once its motion settled, each shown in turn by
    SMIL off one looping clock; the last frame is the still where SMIL doesn't run. No script, no external tool:
    GitHub's img plays it."""
    cfg = cfg or DEFAULTS
    app, frames, head = App(cfg, spawn=lambda *a, **k: None), [], ""
    app.tour = [0.0, 0]
    for i, e in enumerate(TOUR_RUN):
        app.feed(e)
        app.tour[1] = i + 1
        svg = to_svg(render(app.state, size[0], size[1], cfg, app, t=1000.0 + i * TOUR_GAP + 0.9), cfg,
                     title="sampo smith tui --tour")
        head, body = svg.split(">", 1)
        body = body.rsplit("</svg>", 1)[0]
        frames.append(body if not frames else re.sub(r"<style>.*?</style>", "", body, flags=re.S))
    moving = TOUR_GAP * (len(frames) - 1)
    out = [head + ">", f'<rect width="0" height="0"><animate id="tour" attributeName="x" values="0;0" '
                       f'dur="{moving + TOUR_GAP + TOUR_HOLD:g}s" begin="0s;tour.end"/></rect>']
    for i, body in enumerate(frames):
        last = i == len(frames) - 1
        timing = (f'<set attributeName="visibility" to="hidden" begin="tour.begin" dur="{moving:g}s"/>' if last else
                  f'<set attributeName="visibility" to="visible" begin="tour.begin+{i * TOUR_GAP:g}s" '
                  f'dur="{TOUR_GAP:g}s"/>')
        out.append(f'<g visibility="{"visible" if last else "hidden"}">{timing}{body}</g>')
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(out) + "</svg>\n")
    return path


def gallery(cols, height=18, depth=None):
    """T-1311: every theme drawn on SAMPLE_RUN, as many side by side as fit `cols`, each named above its tile; ANSI
    colour at the terminal's depth (0: plain text, as NO_COLOR asks)."""
    depth = colour_depth() if depth is None else depth
    per = max(1, (cols + 2) // 62)  # tiles at least 60 wide (Checks beside Activity), two apart
    tw = max(20, (cols - 2 * (per - 1)) // per)
    tiles = []
    for name in PRESETS:
        cfg = dict(DEFAULTS, theme=PRESETS[name](), preset=name, motion="off")
        app = App(cfg, spawn=lambda *a, **k: None)
        for e in SAMPLE_RUN:
            app.feed(e)
        cv = render(app.state, tw, height, cfg, app)
        rows = Term(depth).cells(cv, cfg) if depth else [[(ch, "") for ch, _ in row] for row in cv.cells]
        tiles.append((name, ["".join(sgr + ch for ch, sgr in row) + ("\x1b[0m" if depth else "") for row in rows]))
    out = []
    for i in range(0, len(tiles), per):
        row = tiles[i:i + per]
        out.append("  ".join(fit(n, tw) + " " * (tw - width(fit(n, tw))) for n, _ in row).rstrip())
        out += ["  ".join(t[1][r] for t in row) for r in range(height)] + [""]
    return "\n".join(out).rstrip("\n")


def snapshot(events, path, size=(120, 34), cfg=None, mode="main"):
    """The screen after these events, as an SVG at path (README pictures, the site, a bug report); a path ending .png
    is the PNG of it (T-1546: needs rsvg-convert)."""
    cfg = cfg or DEFAULTS
    from sampo import core
    app = App(cfg, spawn=lambda *a, **k: None)
    for e in core.redact_obj(list(events)):  # T-1302: a shared file never holds a token a worker printed
        app.feed(e)
    app.mode = mode
    svg = path[:-4] + ".svg" if path.lower().endswith(".png") else path
    with open(svg, "w", encoding="utf-8") as f:  # T-1541: at t=0, never the wall clock, so a build is byte-for-byte
        f.write(to_svg(render(app.state, size[0], size[1], cfg, app, t=0.0), cfg))
    if svg != path:
        _png(svg, path)
    return path


def _png(svg, png):
    """T-1546: svg drawn to png by rsvg-convert; RuntimeError when it isn't installed or fails."""
    tool = shutil.which("rsvg-convert")
    if not tool:
        raise RuntimeError("a PNG needs rsvg-convert (librsvg), which isn't installed; the SVG is written")
    r = subprocess.run([tool, "-o", png, svg], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(f"rsvg-convert failed: {(r.stderr or '').strip()[:200]}")


def photo(app, folder, size, t=None):
    """T-1546, the photo booth: the screen as it stands, as frame-<time>.svg in folder and its PNG beside it when
    rsvg-convert is installed: (svg, png or None, why there's no png)."""
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, "frame-" + time.strftime("%Y%m%d-%H%M%S"))
    k, svg = 1, base + ".svg"
    while os.path.exists(svg):
        k, svg = k + 1, f"{base}-{k}.svg"
    with open(svg, "w", encoding="utf-8") as f:
        f.write(to_svg(render(app.state, size[0], size[1], app.cfg, app, t=t), app.cfg))
    try:
        _png(svg, svg[:-4] + ".png")
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return svg, None, str(e)
    return svg, svg[:-4] + ".png", ""


# T-1546, the chaos twin: what a session might throw at the app, faster than any person
CHAOS_COMMANDS = ("up", "down", "page_up", "page_down", "expand", "follow", "focus", "help", "diff", "history",
                  "thoughts", "search", "new", "projects")  # read-only: none writes a file, runs a command or records
HOSTILE = ["", "x" * 300, "é" * 20 + "́", "表" * 40, "‮abc", "a\tb\nc", "\x1b[31mred\x1b[0m", "🙂" * 30,
           "  ", "​" * 10, "\r\n" * 5, "%s{}{0}", "-" * 1000]


def _chaos_event(rng):
    s = rng.choice(HOSTILE) + rng.choice(["", str(rng.randint(0, 10 ** 9))])
    n = rng.randint
    return rng.choice([
        {"event": "say", "text": s}, {"event": "step", "step": n(-1, 9), "text": s},
        {"event": "plan", "plan": {"type": s, "tier": s, "title": s, "steps": [s] * n(0, 9),
                                   "criteria": [{"text": s, "verify": s}] * n(0, 5)}},
        {"event": "verify", "criterion": n(-1, 6), "label": rng.choice(["before", "verify", s]),
         "exit": rng.choice([0, 1, None, -9]), "cmd": s, "output": s},
        {"event": "changed", "step": n(0, 5), "files": [s] * n(0, 6), "stat": {s: [n(0, 999), n(0, 999)]}},
        {"event": "tokens", "total_in": n(0, 10 ** 7), "total_out": n(0, 10 ** 6), "call_in": n(0, 10 ** 6),
         "call_out": n(0, 10 ** 5)},
        {"event": "action", "action": s, "target": s, "result": s, "detail": s}, {"event": "worker", "text": s},
        {"event": "error", "text": s, "next": s}, {"event": "blocked", "text": s, "reason": s},
        {"event": "task", "task": s}, {"event": rng.choice(["", s, "unknown"]), "text": s}])


def chaos(seconds=10.0, seed=None, clock=time.monotonic):
    """T-1546, the chaos twin: a headless app fed floods of hostile events, random keys (read-only commands, text in
    the prompt and search, Esc) and random sizes for `seconds`, drawing a frame after each:
    {seed, frames, keys, events, resizes, crash}, crash None or {step, size, mode, error, trace}. The seed replays it;
    the app's root and history are an empty temp folder and every source it would read is stubbed."""
    import random
    import tempfile
    import traceback
    seed = random.randrange(1 << 30) if seed is None else seed
    rng, n = random.Random(seed), {"frames": 0, "keys": 0, "events": 0, "resizes": 0}
    keys = [k for k, cmd in DEFAULTS["keys"].items() if cmd in CHAOS_COMMANDS] + ["\x1b", "\x1b", "KEY_LEFT"]
    w, h, step, end = 80, 24, 0, clock() + seconds
    with tempfile.TemporaryDirectory(prefix="fm-chaos-") as tmp:
        none = lambda *a, **k: None  # noqa: E731
        app = App(DEFAULTS, spawn=none, root=tmp, diff=lambda *a, **k: "", history=tmp, choices=lambda *a, **k: [],
                  projects=lambda *a, **k: [], project_state=lambda *a, **k: {}, noticed=lambda *a, **k: [],
                  asker=none, queued=lambda *a, **k: [])
        try:
            while clock() < end:
                step += 1
                roll = rng.random()
                if roll < 0.35:
                    for _ in range(rng.randint(1, 40)):
                        app.feed(_chaos_event(rng))
                        n["events"] += 1
                elif roll < 0.8:
                    typing = app.mode in ("prompt", "search")
                    k = rng.choice("ab Z表🙂-\x7f") if typing and rng.random() < 0.7 else rng.choice(keys)
                    if typing and k in ("\n", "KEY_ENTER"):
                        k = "\x1b"  # a typed request is never sent
                    app.key(k)
                    n["keys"] += 1
                else:
                    w, h = rng.randint(1, 220), rng.randint(1, 70)
                    n["resizes"] += 1
                app.done = False
                render(app.state, w, h, app.cfg, app, t=step * 0.05)
                n["frames"] += 1
        except Exception as e:
            return dict(n, seed=seed, crash={"step": step, "size": [w, h], "mode": app.mode,
                                             "error": f"{type(e).__name__}: {e}"[:300],
                                             "trace": traceback.format_exc()[-1500:]})
    return dict(n, seed=seed, crash=None)


def main(args, words, home=False):
    """fm code tui [REQUEST…] [--plain] [--model M | --worker CLI] [--verify CMD] [--resume [ID]] [--print-config]
    [--snapshot FILE --events FILE]. A bad setting or extension is said once and skipped; --print-config exits 2."""
    from sampo import settings
    ext_errors = load_extensions()
    if getattr(args, "print_config", False):  # what tui.toml says, whether the app can draw with it or not
        (cfg, errors), booted, seen = load_config(profile=False), None, None  # T-1656: its profiles as tables
    else:  # T-1543: one the app can't draw with starts the last good settings, and says so
        cfg, errors, booted, seen = settings.boot()
    problems = [f"extension {e}" for e in ext_errors] + ([booted] if booted else []) + errors
    if getattr(args, "print_config", False):
        print(print_config(cfg, config_path(), settings.profiles()["profiles"]), end="")
    for e in problems:
        print(f"sampo smith tui: {e}", file=sys.stderr)
    if getattr(args, "print_config", False):
        return 2 if errors else 0
    if getattr(args, "chaos", None) is not None:  # T-1546: the chaos twin, headless
        got = chaos(args.chaos, getattr(args, "seed", None))
        crash = got["crash"]
        print(f"chaos: {got['frames']} frames, {got['keys']} keys, {got['events']} events, {got['resizes']} resizes "
              f"in {args.chaos:g} s (seed {got['seed']}): "
              + ("no crash" if not crash else f"crashed at step {crash['step']} in {crash['mode']} at "
                 f"{crash['size'][0]}x{crash['size'][1]}: {crash['error']}\n{crash['trace']}\nreplay: sampo smith tui "
                 f"--chaos {args.chaos:g} --seed {got['seed']}"))
        return 1 if crash else 0
    if getattr(args, "tour", False):  # T-1309: the sample run with captions: on the screen, as lines, or as an SVG
        if getattr(args, "snapshot", None):
            w, _, h = (getattr(args, "size", None) or "110x32").partition("x")
            print(tour_svg(args.snapshot, (int(w), int(h)), cfg))
            return 0
        if getattr(args, "plain", False) or not sys.stdout.isatty() or not sys.stdin.isatty():
            return tour_plain()
        app = App(cfg, spawn=lambda *a, **k: None)
        app.start_tour()
        run_screen(app)
        return 0
    out = getattr(args, "snapshot", None) or getattr(args, "record", None)
    if out:
        src = getattr(args, "events", None)
        if not src:
            print("sampo smith tui --snapshot/--record needs --events FILE (sampo smith … --json > FILE records one)",
                  file=sys.stderr)
            return 2
        with open(src, encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        w, _, h = (getattr(args, "size", None) or "120x34").partition("x")
        if getattr(args, "snapshot", None):
            try:
                print(snapshot(events, out, (int(w), int(h)), cfg))
            except RuntimeError as e:  # T-1546: a .png with no rsvg-convert
                print(f"sampo smith tui: {e}", file=sys.stderr)
                return 2
            return 0
        try:
            print(record(events, out, (int(w), int(h)), cfg=cfg))
        except (RuntimeError, subprocess.CalledProcessError) as e:
            print(f"sampo smith tui: {e}", file=sys.stderr)
            return 2
        return 0
    if getattr(args, "events", None):  # T-1363: a recorded run alone replays: on the screen, else as Activity lines
        return replay_file(args.events, cfg, plain=getattr(args, "plain", False) or not sys.stdout.isatty()
                           or not sys.stdin.isatty())
    from sampo import cli as scli
    try:
        p = scli.resolve(args, create=not home)  # the app never registers a folder by opening in it
    except scli.UsageError:
        if not home:
            raise
        p = None  # T-1330: outside any project the app opens on its home
    opts = {k: getattr(args, k, None) for k in ("model", "worker", "verify", "escalate", "max_steps", "no_commit",
                                                 "worker_timeout", "dry_plan")}
    root = os.path.realpath(p.root) if p else os.getcwd()
    from sampo import core
    app = App(cfg, root=root, opts=opts, history=p and os.path.join(p.dir, "code"),
              used=os.path.join(core.state_dir(), "code-keys.json"),  # T-1348: the launch tip's memory
              place=os.path.join(core.state_dir(), "code-place.json"))
    app.tui = seen  # T-1543: an edit to tui.toml while it runs is read back in
    app.cfg = app._layer(app.cfg)  # T-1611: this project's settings and the repo's pins over tui.toml
    app.sweep()  # T-1756: runners a killed run left, stopped as the app opens
    try:
        from sampo import core
        app.seen_file = os.path.join(core.state_dir(), "code_home_seen")  # T-1344
    except OSError:
        pass
    if home:  # T-1330: inside a project, its view; elsewhere every project
        app.show_home()
        here = next((x for x in app.items if p and x.get("project") == p.slug and x.get("kind") != "noticed"),
                    None)  # a worktree too
        if here:
            app.open_project(here)
        else:
            app.reopen()
        if problems:
            app.message = booted or f"{len(problems)} setting(s) skipped: {problems[0]}"
        run_screen(app)
        for line in app.stop_report():  # T-1756: after the screen is gone, each runner the quit stopped
            print(line, file=sys.stderr)
        return 0
    resume = getattr(args, "resume", None)
    first = (lambda: app.start([], resume=resume or True)) if resume is not None else (
        lambda: app.start([" ".join(words)])) if words else None
    if getattr(args, "plain", False) or not sys.stdout.isatty() or not sys.stdin.isatty():
        if not first:
            print('sampo smith tui --plain needs a request: sampo smith tui --plain "fix the login bug"', file=sys.stderr)
            return 2
        first()
        return run_plain(app)
    if problems:
        app.message = booted or f"{len(problems)} setting(s) skipped: {problems[0]}"
    if first:
        first()
    run_screen(app)
    for line in app.stop_report():  # T-1756
        print(line, file=sys.stderr)
    return 0
