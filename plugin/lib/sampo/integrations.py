"""T-1551: Sampo for other tools, stdlib only and read-only (no network, no state written).
fm timeline: a task's run (its ledger events) as Perfetto / Chrome trace JSON (ui.perfetto.dev, chrome://tracing) or, with
--otlp, OTLP JSON for an OpenTelemetry collector. fm glance: the status as one JSON line for shells and status bars.
fm lsp: a Language Server stub over stdio that publishes a brief's proof gaps as diagnostics."""
import hashlib
import json
import math
import os
import re
import sys

from sampo import core as c

# ---------------------------------------------------------------- fm timeline


def _clean(o):
    """Redacted the way the ledger is (an older Sampo's line may predate it); a non-finite number would be invalid JSON."""
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, list):
        return [_clean(x) for x in o]
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    return c.redact(o)


def _secs(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 else 0.0


def run_of(p, tid):
    """The brief (None once archived) and the task's ledger events as [(POSIX time, event)], in ledger order."""
    b = c.find_brief(p, tid)
    since = (b.meta.get("created") if b else None) or ""  # nothing before the task was made concerns it
    run = []
    for e in c.ledger_events(p, since=since if isinstance(since, str) else ""):
        t = c.parse_ts(e.get("ts"))
        if e.get("task") == tid and t:
            run.append((t.timestamp(), e))
    return b, run


def model(tid, title, run, private):
    """spans [{name, start, end, track, args, error}] and marks [{name, at, track, args}]: the task (track None) with
    its steps laid end to end inside it, each check of a check_run (on its session's track, laid end to end and
    ending at the event), and every event as a mark. private (a sensitive project): timings and kinds, no words."""
    t0, t1 = min(t for t, _ in run), max(t for t, _ in run)  # a line can land after a later one
    spans = [{"name": tid if private or not title else f"{tid} {c.redact(title)}", "start": t0, "end": t1,
              "track": None, "args": {"task": tid}, "error": False}]
    ends, marks, floor = {}, [], {}
    for t, e in run:
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        track = c.redact(str(e.get("session_id") or "no session"))
        if e.get("event") in ("evidence", "step_done") and isinstance(d.get("step"), int):
            ends[d["step"]] = max(t, ends.get(d["step"], t))
        if e.get("event") == "check_run" and isinstance(d.get("results"), list):
            end = t  # gates run one after another; ts is cut to the second, so a run never starts before the last one
            for r in reversed([r for r in d["results"] if isinstance(r, dict)]):  # ended (Perfetto drops overlaps)
                s, code = _secs(r.get("s")), r.get("exit")
                start = max(end - s, min(floor.get(track, end - s), end))
                spans.append({"name": "check" if private else c.redact(str(r.get("cmd")))[:200], "start": start,
                              "end": end, "track": track, "error": bool(code),
                              "args": {"exit": code if isinstance(code, int) else None, "s": s}})
                end = start
            floor[track] = t
        marks.append({"name": str(e.get("event")), "at": t, "track": track, "args": {} if private else _clean(d)})
    prev = t0
    for n, end in sorted(ends.items(), key=lambda kv: kv[1]):
        spans.append({"name": f"step {n}", "start": prev, "end": end, "track": None, "args": {"step": n},
                      "error": False})
        prev = end
    return spans, marks


def perfetto(slug, spans, marks):
    """Chrome trace JSON (the Object Format Perfetto imports): a thread per session, the task's own track first."""
    tracks = {None: 1}
    for x in spans + marks:
        tracks.setdefault(x["track"], len(tracks) + 1)
    us = lambda t: int(round(t * 1e6))  # noqa: E731
    evs = [{"name": "process_name", "ph": "M", "pid": 1, "tid": 1, "args": {"name": f"foreman {slug}"}}]
    evs += [{"name": "thread_name", "ph": "M", "pid": 1, "tid": n, "args": {"name": "task" if k is None else k}}
            for k, n in tracks.items()]
    evs += [{"name": s["name"], "cat": "foreman", "ph": "X", "ts": us(s["start"]),
             "dur": max(0, us(s["end"]) - us(s["start"])), "pid": 1, "tid": tracks[s["track"]], "args": s["args"]}
            for s in spans]
    evs += [{"name": m["name"], "cat": "ledger", "ph": "i", "s": "t", "ts": us(m["at"]), "pid": 1,
             "tid": tracks[m["track"]], "args": m["args"]} for m in marks]
    return {"traceEvents": evs, "displayTimeUnit": "ms"}


def _any(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}


def otlp(slug, tid, spans, marks):
    """OTLP JSON (ExportTraceServiceRequest): the task is the root span, steps and checks its children, the events
    its span events. Ids are hashes of the project and task, so a second export of the same run matches the first."""
    trace = hashlib.sha256(f"{slug} {tid}".encode()).hexdigest()[:32]
    sid = lambda i: hashlib.sha256(f"{trace} {i}".encode()).hexdigest()[:16]  # noqa: E731
    ns = lambda t: str(int(round(t * 1e9)))  # noqa: E731
    attrs = lambda d: [{"key": str(k), "value": _any(v)} for k, v in d.items()]  # noqa: E731
    out = []
    for i, s in enumerate(spans):
        span = {"traceId": trace, "spanId": sid(i), "name": s["name"], "kind": 1, "startTimeUnixNano": ns(s["start"]),
                "endTimeUnixNano": ns(max(s["start"], s["end"])),
                "attributes": attrs(dict(s["args"], **({"foreman.session": s["track"]} if s["track"] else {})))}
        if i:
            span["parentSpanId"] = sid(0)
        if s["error"]:
            span["status"] = {"code": 2}  # STATUS_CODE_ERROR
        out.append(span)
    out[0]["events"] = [{"timeUnixNano": ns(m["at"]), "name": m["name"],
                         "attributes": attrs(dict(m["args"], **{"foreman.session": m["track"]}))} for m in marks]
    return {"resourceSpans": [{"resource": {"attributes": attrs({"service.name": "foreman", "foreman.project": slug})},
                               "scopeSpans": [{"scope": {"name": "foreman"}, "spans": out}]}]}


def cmd_timeline(args):
    from sampo import cli
    p = cli.resolve(args, create=False)
    tid = (args.id or "").upper() or getattr(c.active_brief(c.load_briefs(p), p.lane), "id", "")
    if not tid:
        raise cli.UsageError("no active task here: name one (fm timeline T-0042)")
    if not re.fullmatch(r"T-\d+", tid):
        raise cli.UsageError(f"{args.id!r} isn't a task id (T-0042)")
    b, run = run_of(p, tid)
    if not run:
        raise cli.UsageError(f"no ledger events for {tid} in {p.slug}")
    spans, marks = model(tid, b.title if b else "", run, bool(c.read_meta(p).get("sensitive")))
    doc = otlp(p.slug, tid, spans, marks) if args.otlp else perfetto(p.slug, spans, marks)
    text = json.dumps(doc, ensure_ascii=True)  # escaped: no C1 control or stray surrogate reaches a terminal
    if not args.out:
        return print(text)
    path = os.path.realpath(args.out)  # through a symlink, not over it
    c.write_atomic(path, text + "\n")
    print(f"{path}: {tid}, {len(spans)} spans and {len(marks)} events; "
          + ("POST it to a collector's /v1/traces" if args.otlp else "open it at https://ui.perfetto.dev"))


# ---------------------------------------------------------------- fm glance


def glance(p):
    """One status for any shell or bar: the counts, and waybar's text, tooltip and class (paused, active, blocked, idle)."""
    sd = c.state_dict(p)
    a = sd["active"]
    step = (f"{a['step']['n']}/{a['step']['of']}" if a["step"] else f"{a['steps_done']}/{a['steps_total']}") if a else None
    paused = bool(sd["paused"] or sd["panic"])
    private = bool(c.read_meta(p).get("sensitive"))  # a sensitive project: ids and counts, no words
    title = c.redact(a["title"]) if a and not private else None
    return {"v": 1, "project": p.slug, "task": a and a["id"], "type": a and a["type"], "tier": a and a["tier"],
            "title": title, "step": step, "queue": len(sd["queue"]), "inbox": len(sd["inbox"]),
            "blocked": len(sd["blocked"]), "lanes": len(sd["lanes"]), "paused": paused, "drive": bool(sd["drive"]),
            "autonomy": sd["autonomy"], "text": c.state_line(sd),
            "tooltip": f"{p.slug}: " + (f"{a['id']} {title or ''}".rstrip() if a else "no active task"),
            "class": "paused" if paused else "active" if a else "blocked" if sd["blocked"] else "idle"}


def cmd_glance(args):
    from sampo import cli
    try:
        p = cli.resolve(args, create=False)  # a bar polling outside a project never makes one
    except cli.UsageError:
        if getattr(args, "project", None):
            raise
        return print(json.dumps({"v": 1, "project": None, "text": "", "tooltip": "not in a Sampo project",
                                 "class": "none"}))
    try:
        line = glance(p)
    except Exception as e:  # a bar shows the error, never a trace
        line = {"v": 1, "project": p.slug, "text": "sampo: error", "class": "error",
                "tooltip": c.redact(f"state unreadable ({type(e).__name__}: {e}); run fm doctor")[:300]}
    print(json.dumps(line, ensure_ascii=True))  # escaped: no U+2028 a line reader could split on


# ---------------------------------------------------------------- fm desk (T-1615)

DESK = (800, 480)  # the common 7.5-inch e-paper panel; an old phone scales it
STATES = {"active": "working", "paused": "paused", "blocked": "blocked", "idle": "idle", "error": "can't read state"}


def desk_svg(g, size=DESK, when=None):
    """The glance as a desk card: black on white, large type, no colour and no motion, so an e-paper panel draws it as
    is. The task and its step with a bar, the counts, the state and when it was drawn."""
    import html
    import textwrap
    w, h = size
    s = min(w / 800, h / 480)  # every measure scales with the card
    px = lambda v: f"{v * s:.1f}"  # noqa: E731
    esc = lambda t: html.escape(str(t))  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
           f'font-family="DejaVu Sans, Helvetica, Arial, sans-serif" fill="#000"><rect width="{w}" height="{h}" '
           f'fill="#fff"/>',
           f'<text x="{px(32)}" y="{px(52)}" font-size="{px(22)}">{esc(g.get("project") or "no project")}</text>',
           f'<text x="{w - 32 * s:.1f}" y="{px(52)}" font-size="{px(22)}" text-anchor="end">'
           f'{esc(STATES.get(g.get("class"), g.get("class") or ""))}</text>',
           f'<line x1="{px(32)}" y1="{px(70)}" x2="{w - 32 * s:.1f}" y2="{px(70)}" stroke="#000" stroke-width="{px(2)}"/>']
    if g.get("task"):
        out.append(f'<text x="{px(32)}" y="{px(132)}" font-size="{px(52)}" font-weight="700">{esc(g["task"])}</text>')
        for i, line in enumerate(textwrap.wrap(g.get("title") or "", 34, max_lines=2, placeholder=" …")):
            out.append(f'<text x="{px(32)}" y="{px(182 + i * 40)}" font-size="{px(32)}">{esc(line)}</text>')
        done, _, of = str(g.get("step") or "").partition("/")
        if done.isdigit() and of.isdigit() and int(of):
            bar = (w - 64 * s) * min(1, int(done) / int(of))
            out += [f'<rect x="{px(32)}" y="{px(280)}" width="{w - 64 * s:.1f}" height="{px(28)}" fill="#fff" '
                    f'stroke="#000" stroke-width="{px(3)}"/>',
                    f'<rect x="{px(32)}" y="{px(280)}" width="{bar:.1f}" height="{px(28)}"/>',
                    f'<text x="{px(32)}" y="{px(345)}" font-size="{px(26)}">step {esc(g["step"])}</text>']
    else:
        out.append(f'<text x="{px(32)}" y="{px(150)}" font-size="{px(44)}" font-weight="700">No active task</text>')
    counts = [("queued", g.get("queue", 0)), ("inbox", g.get("inbox", 0)), ("blocked", g.get("blocked", 0)),
              ("lanes", g.get("lanes", 0))]
    col = (w - 64 * s) / len(counts)
    for i, (name, n) in enumerate(counts):
        x = 32 * s + i * col
        out += [f'<text x="{x:.1f}" y="{px(420)}" font-size="{px(40)}" font-weight="700">{esc(n)}</text>',
                f'<text x="{x:.1f}" y="{px(452)}" font-size="{px(20)}">{name}</text>']
    if when:
        out.append(f'<text x="{w - 32 * s:.1f}" y="{px(345)}" font-size="{px(20)}" text-anchor="end">'
                   f'at {esc(when)}</text>')
    return "".join(out) + "</svg>\n"


def desk_html(svg, every=60):
    """The card as a page an old phone keeps open: it reloads itself every `every` seconds."""
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,"
            f"initial-scale=1\"><meta http-equiv=\"refresh\" content=\"{max(10, int(every))}\"><title>Sampo desk"
            "</title><style>html,body{margin:0;background:#fff}svg{display:block;width:100vw;height:auto}</style>"
            f"</head><body>{svg}</body></html>\n")


def _desk_write(path, svg, every):
    """The card to `path`: .html a page, .png through rsvg-convert, else the SVG; replaced whole, so a device never
    reads half a card."""
    if path.endswith(".png"):
        import shutil
        import subprocess
        if not shutil.which("rsvg-convert"):
            raise RuntimeError("a PNG card needs rsvg-convert (librsvg), which isn't installed; --out FILE.svg works")
        tmp = path + ".svg.tmp"
        c.write_atomic(tmp, svg)
        try:
            subprocess.run(["rsvg-convert", "-o", path + ".tmp", tmp], check=True, timeout=60,
                           stdin=subprocess.DEVNULL, capture_output=True)
            os.replace(path + ".tmp", path)
        finally:
            for f in (tmp, path + ".tmp"):
                if os.path.exists(f):
                    os.unlink(f)
        return
    c.write_atomic(path, desk_html(svg, every or 60) if path.endswith(".html") else svg)


def cmd_desk(args):
    """fm desk [--out FILE] [--size WxH] [--every S]: the glance as a desk card for an e-paper panel or an old phone.
    Nothing listens on a port: the card is a file (SVG, .png with rsvg-convert, .html that reloads itself) that a
    synced folder carries to the device; --every rewrites it every S seconds until Ctrl-C."""
    import subprocess
    import time
    from sampo import cli
    try:
        w, _, h = (args.size or f"{DESK[0]}x{DESK[1]}").lower().partition("x")
        size = (int(w), int(h))
    except ValueError:
        size = (0, 0)
    if not (120 <= size[0] <= 4000 and 120 <= size[1] <= 4000):
        raise cli.UsageError("--size takes WxH, each 120 to 4000 (800x480 is the common 7.5-inch e-paper panel)")
    every = getattr(args, "every", None)
    if every is not None and every < 10:
        raise cli.UsageError("--every takes 10 seconds or more (an e-paper panel wears with each refresh)")
    p = cli.resolve(args, create=False)
    while True:
        try:
            g = glance(p)
        except Exception as e:  # a card shows the error, never a trace
            g = {"project": p.slug, "class": "error", "title": c.redact(f"{type(e).__name__}: {e}")[:80]}
        svg = desk_svg(g, size, time.strftime("%H:%M"))
        if not args.out:
            print(svg, end="")
            return 0
        try:
            _desk_write(args.out, svg, every)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise cli.UsageError(str(e))
        if every is None:
            print(f"wrote {args.out} ({size[0]}x{size[1]})")
            return 0
        try:
            time.sleep(every)
        except KeyboardInterrupt:
            return 0


# ---------------------------------------------------------------- fm lsp

MAX_BODY = 32 << 20  # a frame bigger than this ends the session (no client sends a 32 MB brief)
LINE_MAX = 2000  # characters of a line gaps() matches
WARNING, INFO = 2, 3


def _u16(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2  # LSP columns count UTF-16 code units


def gaps(text):
    """A brief's proof gaps as LSP diagnostics: a criterion with no verify command or no evidence, a step with no
    evidence (no test run recorded). Text that isn't a brief has none."""
    if not isinstance(text, str):
        return []
    text = text.replace("\r\n", "\n")
    try:
        b = c.Brief.parse(text)
    except ValueError:
        return []
    tid, out, section, ac = b.id or "ID", [], None, 0
    proven = {(m.group(1), int(m.group(2))) for x in b.evidence() if (m := c._EV_RE.match(x))}  # has_evidence, once
    lines = text.split("\n")
    for i in range(text[:text.find("\n---\n", 4) + 5].count("\n"), len(lines)):
        # ponytail: fmcore's step and criterion patterns backtrack quadratically on a long line, so only its first
        # LINE_MAX characters are read (a verify command past them reads as missing) until they are fixed there
        line, hit = lines[i][:LINE_MAX], None
        if line.startswith("## "):
            section = line[3:].strip()
        elif section == "Acceptance criteria" and (m := c._AC_RE.match(line)):
            ac += 1
            if not c.verify_of(m.group(2)):
                hit = WARNING, f"criterion {ac} has no verify command, so nothing can prove it"
            elif ("ac", ac) not in proven:
                hit = (WARNING if m.group(1) in "xX" else INFO,
                       f"criterion {ac} has no evidence yet: fm task evidence {tid} --ac {ac} --run \"<cmd>\"")
        elif section == "Steps" and (m := c._STEP_RE.match(line)) and ("step", int(m.group(1))) not in proven:
            n = int(m.group(1))
            hit = (WARNING, f"step {n} is ticked but has no evidence: no test run recorded for it") if m.group(2) in "xX" \
                else (INFO, f"step {n} has no evidence yet: fm task evidence {tid} --step {n} --run \"<test>\"")
        if hit:
            out.append({"range": {"start": {"line": i, "character": 0}, "end": {"line": i, "character": _u16(lines[i])}},
                        "severity": hit[0], "source": "foreman", "message": hit[1]})
    return out


def _frames(inp):
    """Each message body in turn (None for a frame without a usable Content-Length); ends at EOF or an oversized frame."""
    while True:
        n, seen = None, False
        while True:
            line = inp.readline(65536)
            if not line:
                return
            if not line.strip():
                if seen:
                    break
                continue  # blank lines between frames
            seen = True
            k, _, v = line.partition(b":")
            if k.strip().lower() == b"content-length" and v.strip().isdigit() and len(v.strip()) <= 10:
                n = int(v)
        if n is None:
            yield None
            continue
        if n > MAX_BODY:
            return
        body = inp.read(n)
        if len(body) < n:
            return
        yield body


def serve(inp, out):
    """The stub: initialize, shutdown and exit; didOpen, didChange, didSave (with text) and didClose publish the
    document's proof gaps. Full sync, so no document is kept. Exit code 0 only after a shutdown."""
    def send(msg):
        body = json.dumps(dict(msg, jsonrpc="2.0")).encode()  # ASCII: a client's lone surrogate can't fail the encode
        out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        out.flush()

    def publish(uri, text):
        if isinstance(uri, str):
            try:
                found = gaps(text)
            except Exception:  # a diagnostic is a hint: odd text must not end the editor's session
                found = []
            send({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": found}})

    down = False
    for body in _frames(inp):
        try:
            msg = json.loads(body) if body is not None else None
        except (ValueError, RecursionError):  # bad JSON or UTF-8, or nesting deeper than Python recurses
            msg = None
        if not isinstance(msg, dict):
            send({"id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        method, params = msg.get("method"), msg.get("params") if isinstance(msg.get("params"), dict) else {}
        doc = params.get("textDocument") if isinstance(params.get("textDocument"), dict) else {}
        changes = params.get("contentChanges")
        if method == "initialize":
            send({"id": msg.get("id"), "result": {
                "capabilities": {"textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": True}}},
                "serverInfo": {"name": "foreman"}}})
        elif method == "shutdown":
            down = True
            send({"id": msg.get("id"), "result": None})
        elif method == "exit":
            return 0 if down else 1
        elif method == "textDocument/didOpen":
            publish(doc.get("uri"), doc.get("text"))
        elif method == "textDocument/didChange" and isinstance(changes, list) and changes \
                and isinstance(changes[-1], dict):
            publish(doc.get("uri"), changes[-1].get("text"))
        elif method == "textDocument/didSave" and "text" in params:
            publish(doc.get("uri"), params["text"])
        elif method == "textDocument/didClose":
            publish(doc.get("uri"), None)
        elif "id" in msg and isinstance(method, str):
            send({"id": msg["id"], "error": {"code": -32601, "message": f"method not found: {method}"[:200]}})
    return 0 if down else 1


def cmd_lsp(args):
    return serve(sys.stdin.buffer, sys.stdout.buffer)
