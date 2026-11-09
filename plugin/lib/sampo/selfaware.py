"""T-3126 (T-3123 part 1): the miss ledger. A user message that points something out ("why isn't X done", "are you also
tracking Y", "have you tried Z") is a thing Sampo should have caught itself. The UserPromptSubmit hook runs `note`:
plain patterns pick the class; a message with no pattern is never sent to a model from the hook (latency on every
turn). `sampo misses` counts the logged `miss` events by class and day; `sampo misses --sweep` asks the user's local
model about recent unmatched questions, naming only the fixed classes below (the model's answer is accepted only if it
is one of them; no Claude-written label ever trains or decides anything). A request or a thanks is not a miss."""
import contextlib
import os
import re

from sampo import core as c

CLASSES = ("stalled", "unfiled", "unmeasured", "invisible", "unreachable", "unused", "dead", "unbuilt", "other")
# first match wins; each row is (class, pattern) seeded from the user's own pointing-out messages
PATTERNS = [
    ("stalled", r"\bwhy (is|isn'?t|are|aren'?t|hasn'?t|haven'?t|was|wasn'?t)\b.*\b(done|finished|ready|built|complete|yet)\b"),
    ("stalled", r"\b(still not|not) (done|finished|ready) yet\b"),
    ("unfiled", r"\bare you (also |still )?(keeping track|tracking|fixing|filing|capturing|logging|noting)\b"),
    ("unfiled", r"\b(bugs|problems|issues|workarounds?) you (come|came|run|ran|hit|noticed|see)\b"),
    ("unmeasured", r"\bhow (can|could|do|would) (we|i|you) (improve|speed|make|reduce|cut)\b.*"
                   r"\b(speed|fast|efficien|slow|queue|wait|latency|time)\b"),
    ("unused", r"\bhave you (tried|used|considered|looked at|thought about)\b"),
    ("invisible", r"\bhow (can|could|do|would) i (view|see|find|open|get to|look at|browse)\b"),
    ("unreachable", r"\bwhy (can'?t|couldn'?t|won'?t|doesn'?t|don'?t) (you|we|it|sampo)\b"),
    ("dead", r"\b(is|are|was) (it|that|this|the (job|run|task|build|lane|agent)s?)\b.*\b(stuck|hung|dead|frozen|still running)\b"),
    ("dead", r"\bnothing (is )?(happening|moving|running)\b"),
    # T-3931: asks phrased as a request or a question about a capability (2026-11-08's seven)
    ("unbuilt", r"\bmake\b.{0,40}\ba feature\b|\bif it isn'?t already\b|\bif i don'?t say (anything|so)\b"
                r"|\bwould it be (good|better|worth|useful|nice)\b.*\bto have\b"),
    ("unused", r"\bhas\b.{0,60}\b(actually |ever )?been (used|tried)\b"),
    ("unmeasured", r"\bare you (also |still )?(measuring|monitoring|evaluating|benchmarking|scoring)\b"),
    ("unfiled", r"\bnot (matching|lining) up\b|\bcan you (also )?(have it |make it )?fix (a few|some|the)\b"),
    ("other", r"\b(make|making) yourself\b|\bself.?aware\b|\bso i (don'?t|do not) (need|have) to (point|tell|remind|ask)\b"
             r"|\byou (missed|forgot|should have|didn'?t notice)\b|\bwhy\b.{0,40}\bmissed\b"),
]
_PATTERNS = [(k, re.compile(p, re.I)) for k, p in PATTERNS]
_SWEEPABLE = re.compile(r"\?\s*$|\byou\b", re.I)  # a question or one aimed at Sampo; the rest is a request or chat
_THANKS = re.compile(r"^\W*(thanks?|thank you|thx|ty|great|nice|ok(ay)?|cool|perfect|yes|no)\W*$", re.I)
MAX_TEXT = 300


def classify(text, ask=None):
    """The miss class of a message, or None. Patterns first; `ask(text)` (a model's one-word answer) only when none
    matched and the message could be aimed at Sampo; an answer outside CLASSES counts as no miss."""
    text = (text or "").strip()
    if not text or _THANKS.match(text):
        return None
    for cls, rx in _PATTERNS:
        if rx.search(text):
            return cls
    if ask and _SWEEPABLE.search(text):
        got = str(ask(text) or "").strip().lower().strip(".")
        return got if got in CLASSES else None
    return None


def note(p, text, session=None, ask=None):
    """Log a `miss` event when the message points something out; returns the class or None. Never raises."""
    try:
        cls = classify(text, ask)
        if cls:
            c.log_event(p, "miss", data={"cls": cls, "text": c.fit(c.redact(c.plain(text)), MAX_TEXT)}, session=session)
        return cls
    except Exception:
        return None


def report(p, days=30):
    """{total, by_class, by_day} over the last `days` days of `miss` events."""
    from datetime import timedelta
    since =(c.clock() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    by_class, by_day = {}, {}
    for e in c.ledger_events(p, ["miss"], since=since):
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        cls = d.get("cls") if d.get("cls") in CLASSES else "other"
        by_class[cls] = by_class.get(cls, 0) + 1
        day = str(e.get("ts") or "")[:10]
        by_day[day] = by_day.get(day, 0) + 1
    return {"total": sum(by_class.values()), "by_class": by_class, "by_day": dict(sorted(by_day.items())),
            "catches": len(c.ledger_events(p, ["predicted_catch"], since=since))}


def local_ask(p):
    """A one-word classifier on the user's local model, or None when none is up. The prompt names only CLASSES."""
    from sampo import models, moonscale
    prof = moonscale._local(bool(c.read_meta(p).get("sensitive")))
    if not prof:
        return None
    system = ("A user wrote this to their coding assistant. If it points out something the assistant should have "
              "noticed itself, answer with exactly one word from: " + ", ".join(CLASSES)
              + ". If it is a request, a thanks or plain chat, answer: none.")

    def ask(text):
        try:
            r = models.complete(prof, [{"role": "system", "content": system}, {"role": "user", "content": text}],
                                max_tokens=8, timeout=60, think=False)
        except (models.ModelError, OSError, ValueError):
            return None
        return (r or {}).get("text")
    return ask


def sweep(p, days=3):
    """Ask the local model about recent prompts no pattern matched and that were never noted; returns the count
    logged. Idempotent: a prompt already seen (by its text) is skipped."""
    ask = local_ask(p)
    if not ask:
        return 0
    from datetime import timedelta
    since = (c.clock() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    seen = {(e.get("data") or {}).get("text") for e in c.ledger_events(p, ["miss", "miss_checked"], since=since)}
    n = 0
    for e in c.ledger_events(p, ["prompt"], since=since):
        text = c.fit(c.redact(c.plain((e.get("data") or {}).get("text") or "")), MAX_TEXT)
        if text in seen or classify(text) or not _SWEEPABLE.search(text) or _THANKS.match(text):
            continue
        seen.add(text)
        if note(p, text, session=e.get("session_id"), ask=ask):
            n += 1
        else:
            c.log_event(p, "miss_checked", data={"text": text}, session=e.get("session_id"))
    return n


def cmd_misses(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "why", False):
        return _cmd_why(args, p)
    swept = sweep(p) if getattr(args, "sweep", False) else 0
    r = report(p, getattr(args, "days", 30))
    lines = [f"{r['total']} misses in the last {getattr(args, 'days', 30)} days"
             + (f" ({swept} found by the local model)" if swept else "")]
    lines += [f"  {k:<11} {v}" for k, v in sorted(r["by_class"].items(), key=lambda kv: -kv[1])]
    if r["by_day"]:
        lines.append("per day:")
        lines += [f"  {d}  {n}  {'#' * min(n, 40)}" for d, n in r["by_day"].items()]
    lines.append(f"{r['catches']} catch{'es' if r['catches'] != 1 else ''} (asks Sampo predicted and filed before "
                 f"the user made them)")
    cli.out(args, r, "\n".join(lines))


def _cmd_why(args, p):
    from sampo import cli
    r = why(p, getattr(args, "days", 30), capture=not getattr(args, "no_capture", False),
            remote=not getattr(args, "local", False), judge=True)
    good = [x for x in r["records"] if not x.get("rejected")]
    lines = [f"{len(good)} misses with a why ({len(r['records']) - len(good)} rejected)"]
    for x in good:
        lines.append(f"  [{x['cause']}] {c.fit(x['text'], 70)}" + (f" ({x['remote']})" if x.get("remote") else ""))
        lines += [f"      {s}" for s in x["chain"]]
    lines.append("roots (misses behind each):")
    for root, n in sorted(r["roots"].items(), key=lambda kv: -kv[1]):
        d = r["detectors"].get(root)
        lines.append(f"  root: {root}  {n}" + (f"  -> detector {d['task']}" if d else ""))
    lines.append("per day, misses by cause vs catches:")
    for day, row in r["trend"].items():
        lines.append(f"  {day}  " + "  ".join(f"{k} {v}" for k, v in sorted(row.items())))
    if r["remotes_failed"]:
        lines.append("remotes not reached: " + ", ".join(r["remotes_failed"]))
    cli.out(args, r, "\n".join(lines))


# ---- T-3128 (T-3123 part 3): the boundary review. No model on the close path: it reads the predictions the tick keeps.
BOUNDARY_CAP, COVERED = 3, 0.6  # captures per boundary; share of a guess's words an open task or a user message must hold


def predicted_asks(p):
    """[(type, text)] the next-request predictor last guessed, best first: the local arm's words, then the structural
    guess's type and files. Reads the stored rows only (foresight.prediction_tick wrote them)."""
    from sampo import foresight
    rows = foresight._book(p)
    out = []
    for r in [r for r in rows if r.get("kind") == "next-request-local"][-1:]:
        pr = r.get("pred") or {}
        if pr.get("words") and pr.get("type") in c.TYPES:
            out.append((pr["type"], " ".join(pr["words"])))
    for r in [r for r in rows if r.get("kind") == "next-request"][-1:]:
        pr = r.get("pred") or {}
        if pr.get("type") in c.TYPES and pr.get("paths"):
            out.append((pr["type"], "work on " + c.joinmore(pr["paths"], 3)))
    return out


MIN_GUESS, MIN_MATCH, OPEN_CAP, REFILE_DAYS = 3, 2, 3, 7  # words a guess needs, words that must match, open predicted captures, days a title stays filed
FLAG = "predicted_open"  # p.dir marker: a boundary filed something; catch() reads no briefs while it is absent


def _held(ask_words, text):
    from sampo import foresight
    hit = len(ask_words & foresight._words(text))
    return len(ask_words) >= MIN_GUESS and hit >= MIN_MATCH and hit / len(ask_words) >= COVERED


def boundary(p):
    """At a task boundary (a close or a drop): the top BOUNDARY_CAP predicted asks no open task covers become inbox
    captures with source 'predicted' (eco.capture_once also dedupes an identical open title). Returns the briefs filed."""
    if c.read_meta(p).get("sensitive"):
        return []
    from sampo import eco, foresight
    open_text = [" ".join([b.title, b.section("Raw request")] + [a.text for a in b.acceptance()])
                 for b in c.load_briefs(p) if b.status not in c.CLOSED]
    every = c.load_briefs(p, include_archive=True)
    room = OPEN_CAP - sum(1 for b in every if b.meta.get("source") == "predicted" and b.status not in c.CLOSED)
    recent = {" ".join(b.title.lower().split()) for b in every if b.meta.get("source") == "predicted"
              and (c.age_days(b.meta.get("created")) or 0) < REFILE_DAYS}  # any status: a dropped guess stays dropped
    filed = []
    for typ, text in predicted_asks(p):
        if len(filed) >= min(BOUNDARY_CAP, room):
            break
        text = c.fit(c.redact(c.plain(text)), 160)
        if " ".join(text.lower().split()) in recent or any(_held(foresight._words(text), t) for t in open_text):
            continue
        b, new = eco.capture_once(p, text, typ, "predicted")
        if new:
            filed.append(b)
            open_text.append(b.title)
    if filed:
        c.write_atomic(os.path.join(p.dir, FLAG), "1")
    return filed


def catch(p, text):
    """A user message that matches an open predicted capture is a catch: logs `predicted_catch`, marks the capture;
    returns the ids caught (each once)."""
    from sampo import foresight
    got, flag = [], os.path.join(p.dir, FLAG)
    if not os.path.exists(flag):
        return got
    live = False
    for b in c.load_briefs(p):
        live = live or (b.meta.get("source") == "predicted" and b.status not in c.CLOSED)
        if b.meta.get("source") == "predicted" and b.status not in c.CLOSED and not b.meta.get("caught") \
                and _held(foresight._words(b.title), text):
            b.meta["caught"] = c.now()
            c.save_brief(p, b)
            c.log_event(p, "predicted_catch", task=b.id, data={"text": c.fit(c.redact(c.plain(text)), MAX_TEXT)})
            got.append(b.id)
    if not live:
        with contextlib.suppress(OSError):
            os.remove(flag)
    return got


# ---- T-3931: why was it missed. Each miss gets a cause class from local evidence and a chain ending at a structural root.
ROOTS = ("missing feedback loop", "wrong-way default", "nobody in the user's seat", "lesson without tripwire")
SURFACE = {"unmeasured", "unreachable", "unused", "unfiled", "invisible", "stalled", "dead", "unbuilt", "unknown",
           "not measured", "not reachable", "blind spot", "no signal"}  # a surface class is not a root
CLOSED_STATUS = {"done", "dropped", "closed", "archived", "rejected", "merged", "cancelled"}
LENS = {"stalled": "scout lens: work that sat open", "unfiled": "scout lens: bugs a session saw and did not file",
        "unmeasured": "brainstorm question: what is a number nobody records?",
        "unused": "brainstorm question: what did we build that nothing calls?",
        "invisible": "brainstorm question: what can the user not see?",
        "unreachable": "brainstorm question: what does the user ask that no verb answers?",
        "dead": "scout lens: jobs that stopped", "unbuilt": "brainstorm question: which request is a feature we lack?",
        "other": "brainstorm question: what would the user point out next?"}
DETECTOR = {"known": "a prompt-time tripwire: surface open captures that hold the ask's words before the session answers",
            "signal": "a check over events/journal that fires on this evidence and files the ask",
            "built": "a which-hint: route asks like this to the verb or skill that already does it",
            "blind-spot": "a scout lens or brainstorm question that asks this before the user does"}
ROOT_OF = {"known": ROOTS[1], "signal": ROOTS[0], "built": ROOTS[3], "blind-spot": ROOTS[2]}  # fallback; _cause derives
_QUIET = {"miss", "miss_checked", "prompt", "tool_search", "predicted_catch"}


def catchers():
    """{name: kind} of what could catch a miss, read from the code: verbs (cli.py), hooks (hooks.py), doctor checks and
    night jobs. A name a mapping cites must be here, so a hint never points at something that does not exist."""
    out = {}
    for fname, rx, pre in (("cli.py", r'\badd\("([\w-]+)"', "verb"), ("hooks.py", r"(?m)^def ([a-z]\w*)\(", "hook"),
                           ("doctor.py", r"(?m)^def (check_\w+)\(", "doctor"), ("night.py", r'\("([\w ]+)", \[', "night")):
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), fname), encoding="utf-8") as f:
                out.update({f"{pre}:{n}": pre for n in re.findall(rx, f.read())})
        except OSError:
            pass
    return out


def valid_chain(r, have=None):
    """A why is kept only if its chain has 3+ links, ends at its named root and cites a catcher the code has; the root
    must be a seeded one or a new phrase of 3+ words (the vocabulary grows) and never a surface class."""
    chain, root = r.get("chain"), str(r.get("root") or "")
    return (isinstance(chain, list) and len(chain) >= 3 and all(isinstance(x, str) and x.strip() for x in chain)
            and chain[-1] == f"root: {root}" and root.lower() not in SURFACE and root not in CLASSES
            and (root in ROOTS or len(root.split()) >= 3) and r.get("catcher") in (have or catchers()))


FILLER = """why how can could would should you your youre dont doesnt isnt didnt cant wont what which when who where
whats thats there here make way ways need needs also maybe just like thing things stuff whatever something anything
everything want wanted get got getting have been being able know think see look going really actually ever every
come keep give find does doing please sure kind sort lot lots even still much many well good better best use
implement figure unasked covered idea ideas whole entire instead rather without within around improve improving more less different same new""".split()
MIN_SHARED, MIN_RATIO = 2, 0.5  # a match: 2+ content words shared and half the ask's weight in them


def _content(text):
    """The ask's content words: which's stems less question and filler words (a way to, why do you, how can we)."""
    from sampo import recall, which
    fill = {recall.stem(w, which.STEM) for w in FILLER}
    return {w for w in which.words(text) if w not in fill}


def _score(a, t, idf):
    """(shared, ratio): content words an ask shares with a text, and the share of the ask's idf weight they hold, so
    a word every brief has (agents, sampo) counts little and cpu or batch counts much. (0, 0) below the floors."""
    hit = a & t
    top = sorted(a, key=lambda w: (-round(idf(w), 6), w not in hit, w))[:4]  # ties go to a shared word, then the alphabet; a long ask is judged on its four heaviest words, not diluted by the rest
    ratio = sum(idf(w) for w in top if w in hit) / (sum(idf(w) for w in top) or 1)
    both = all(w in hit for w in top[:2])  # the match holds the ask's two rarest words (cpu, batch), not only agents/sampo
    ok = len(hit) >= MIN_SHARED and ((len(hit) >= 3 and ratio >= MIN_RATIO) or (both and ratio >= 0.6))
    return (len(hit), round(ratio, 2)) if ok else (0, 0)


def _days(a, b):
    ta, tb = c.parse_ts(a), c.parse_ts(b)
    return max(0, round((tb - ta).total_seconds() / 86400)) if ta and tb else None


def _corpus(p, since):
    """(known, signal): rows {ts, id, kind, status, text, words} of what Sampo held (briefs, research) and logged (events)
    before an ask; each row's word set is made once."""
    import json
    from sampo import which
    known, signal = [], []
    for b in c.load_briefs(p, include_archive=True):
        t = b.title + " " + b.section("Raw request")[:240]  # what the brief is about, not every word it quotes
        known.append({"ts": str(b.meta.get("created") or ""), "id": b.id, "kind": "brief", "status": b.status, "text": t,
                      "words": set(which.words(t))})
    rdir = os.path.join(p.dir, "research")
    for n in sorted(os.listdir(rdir))[:200] if os.path.isdir(rdir) else []:
        with contextlib.suppress(OSError):
            with open(os.path.join(rdir, n), encoding="utf-8", errors="replace") as f:
                t = f.read(300)
            known.append({"ts": "", "id": f"research/{n}", "kind": "research", "status": "", "text": t,
                          "words": set(which.words(t))})  # undated: counted as held
    for e in c.ledger_events(p, None, since=since):
        if e.get("event") not in _QUIET:
            t = json.dumps(e.get("data") or {}, default=str)[:400]
            signal.append({"ts": str(e.get("ts") or ""), "id": str(e.get("event")), "kind": "event", "status": "",
                           "text": t, "words": set(which.words(t))})
    return known, signal


def _consumers():
    """{event kind: [module]} the lib's code reads from the ledger by name: what could have acted on a signal."""
    out, d = {}, os.path.dirname(os.path.abspath(__file__))
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".py") and fn not in ("core.py", "selfaware.py"):
            with contextlib.suppress(OSError):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    for m in re.findall(r"ledger_events\([^)\n]*?\[([^\]]*)\]", f.read()):
                        for k in re.findall(r'"([\w-]+)"', m):
                            out.setdefault(k, []).append(fn[:-3])
    return out


def _cause(ask, ts, known, signal, ctx):
    """(cause, evidence, link2, link3, root): known, signal, built or blind-spot, each link read off the miss's own
    evidence. A search that cannot find a corpus line in itself is broken, so a blind spot says so (canary, T-2674)."""
    from sampo import which
    a, best, top = _content(ask), (0, 0), None
    for label, rows in (("known", known), ("signal", signal)):
        for r in rows:
            sc = _score(a, r["words"], ctx["idf"]) if a else (0, 0)
            if r["ts"] < ts and sc > best:  # the best match by weight, not the first
                best, top = sc, (label, r)
    if top:
        label, r = top
        ev = (f'{r["id"]} {r["status"] or r["kind"]}: "{c.fit(c.plain(r["text"]), 90)}" '
              f"(match {best[0]} of {len(a)} content words, weight {best[1]})")
        age = _days(r["ts"], ts)
        when = f"{age} days before the ask" if age is not None else "undated, held before the ask"
        if label == "signal":
            got = ctx["consumers"].get(r["id"])
            if got:
                cat = f"verb:{got[0]}" if f"verb:{got[0]}" in ctx["have"] else "hook:user_prompt_submit"
                return label, ev, f"event: {r['id']} logged {when}; {c.joinmore(got, 3)} read it by name", (
                    f"why not: {got[0]} reads '{r['id']}' events but not against the ask's words"), ROOTS[3], cat
            return label, ev, f"event: {r['id']} logged {when}", (
                f"why not: no hook, next line or night job reads '{r['id']}' events, so it stayed a log line"
            ), ROOTS[0], "hook:user_prompt_submit"
        if r["kind"] == "brief" and r["status"] not in CLOSED_STATUS:
            opens = [x for x in known if x["kind"] == "brief" and x["status"] not in CLOSED_STATUS and x["ts"] < ts]
            pos = 1 + sum(x["ts"] > r["ts"] for x in opens)
            return label, ev, f"held: {r['id']} captured {when}, status {r['status']}, {pos} of {len(opens)} open", (
                f"why not: still open and {pos} of {len(opens)} by newest, so the inbox order buried it and "
                "nothing read it against the prompt"), ROOTS[1], "verb:next"  # next's inbox line ranks it
        if r["kind"] == "brief":
            return label, ev, f"held: {r['id']} was {r['status']} {when}", (
                f"why not: {r['id']} was {r['status']} and nothing reopened it when the ask came back"
            ), ROOTS[3], "verb:outcomes"  # the reopen/regression path (reverts)
        return label, ev, f"held: {r['id']} sat on the research shelf {when}", (
            f"why not: {r['id']} was never recalled against the ask"), ROOTS[3], "verb:recall"
    got = [(k, x) for k, rows in ctx["rank"](ask).items() for x in rows if k in ("skills", "commands")]
    for k, x in got:
        name = str(x.get("name"))
        if set(which.words(name.replace("-", " "))) & a:  # a content word of the ask is in the name
            return "built", f"{k} {name} (score {x.get('score')}) already covers it", (
                f"shelf: {name} exists; the session never searched it for the user's words"), (
                f"why not: which was not run on this ask, so {name} stayed unused"), ROOTS[3], "verb:which"
    probe = (known + signal)[:1]  # canary: a search that cannot find a row in itself is broken (T-2674)
    ok = all(_score(r["words"], r["words"], ctx["idf"]) != (0, 0) or len(r["words"]) < 2 for r in probe)
    ev = (f"no row of {len(known)} briefs/research and {len(signal)} events holds half the weight of its {len(a)} content "
          "words") + ("" if ok else " (search canary failed: unverified)")
    return "blind-spot", ev, f"gap: nothing held or logged before the ask holds its words ({len(known) + len(signal)} rows)", (
        "why not: no scout lens or brainstorm question covers this ground"), ROOTS[2], "night:scout"


# ---- retrieve -> judge -> verify (T-3931 round 4): a local model reads the loosely retrieved rows and picks one; its
# words are kept only when every quoted phrase is in the picked row. Anything else falls back to the rules above.
JUDGE_TIMEOUT, CACHE_MAX, VEC_MAX, RRF_K = 60, 500, 8000, 60
COS_MIN = 0.575  # ponytail: least ask-row cosine (bge-m3) a pick may have: the 7 asks of 2026-11-08 put hand-checked catchers at .60-.69 and off-topic picks at .52-.56; recalibrate per embedder
QUOTA = {"brief": 3, "event": 2, "verb": 3, "skill": 1}  # per kind, so one kind of row cannot fill the eight
_QUOTE = re.compile(r"[\"“']([^\"”']{6,})[\"”']")


def _keys(text):
    """Loose retrieval keys: the first four letters of each stemmed content word (used ~ usage, measuring ~ measure)."""
    from sampo import which
    return {w[:4] for w in which.words(text) if len(w) >= 3}


def _pool(known, signal, found):
    """Rows a judge may pick: briefs and research, one row per event kind (count, last sample; the night's rows say what
    ran or was skipped), every verb's help line and every skill."""
    pool = [dict(r, shown=c.fit(c.plain(r["text"]), 320)) for r in known]
    by = {}
    for r in signal:
        by.setdefault(r["id"], []).append(r)
    for kind, rows in by.items():
        t = f"event {kind}: first seen {rows[0]['ts'][:10]}: {rows[0]['text']}"  # the first row: never one logged after the ask
        pool.append({"ts": rows[0]["ts"], "id": kind, "kind": "event", "status": "", "text": t,
                     "shown": c.fit(c.plain(t), 320)})
    for k, name in (("verb", "commands"), ("skill", "skills")):
        for x in found.get(name) or []:
            t = f"{x['name']}: {x.get('summary') or x.get('text') or ''}"
            pool.append({"ts": "", "id": str(x["name"]), "kind": k, "status": "", "text": t, "shown": c.fit(c.plain(t), 320)})
    for r in pool:
        r["keys"] = _keys(r["text"])
    return pool


def _retrieve(ask, ts, pool, df, sims=None):
    """(rows, cosines): the best eight rows held before the ask, with quotas per kind. The order is the stem-prefix rank
    (overlap weighted by rarity) fused by reciprocal rank (k=60) with the embedding rank when `sims` (the ask's cosine
    to each pool row) is given; then the cosines are each row's, else empty."""
    import math
    a, n = _keys(ask) - {w[:4] for w in FILLER}, len(pool)
    ok = [i for i, r in enumerate(pool) if r["kind"] in ("verb", "skill") or r["ts"] < ts or not r["ts"]]
    stem = sorted(((sum(math.log(1 + n / max(1, df.get(k, 0))) for k in a & pool[i]["keys"]), i) for i in ok
                   if a & pool[i]["keys"]), reverse=True)
    fused = {}
    for rank, (_, i) in enumerate(stem):
        fused[i] = 1 / (RRF_K + rank + 1)
    if sims:
        for rank, i in enumerate(sorted(ok, key=lambda i: -sims[i])):
            fused[i] = fused.get(i, 0) + 1 / (RRF_K + rank + 1)
    out, used = [], {}
    for i in sorted(fused, key=lambda i: (-fused[i], i)):
        k = "brief" if pool[i]["kind"] == "research" else pool[i]["kind"]
        if used.get(k, 0) < QUOTA[k]:
            used[k] = used.get(k, 0) + 1
            out.append(i)
    out = out[:8]
    return [pool[i] for i in out], ([sims[i] for i in out] if sims else [])


def _judge(prof, ask, cands, cache, models):
    """The model's parsed JSON for this ask and candidate set (cached by their hash), or None."""
    import hashlib
    import json
    system = ("A user pointed out something a coding assistant should have noticed itself. Pick the ONE numbered row "
              "that shows what the assistant already had and explain why it did not act: the row closest in topic to the "
              "ask, even when it only half covers it. Answer {\"pick\": null} only when every row is about a different "
              "subject (a row that merely shares a word is one). "
              "Answer JSON only: {\"pick\": number or null, \"link2\": what that row shows, \"link3\": why nothing read "
              "it against the ask, \"root\": one of: " + "; ".join(ROOTS) + "}. link2 and link3 must each quote, in "
              "double quotes, a phrase copied exactly from the picked row.")
    prompt = "Ask: " + ask[:600] + "\n\nRows:\n" + "\n".join(f"{i}. [{x['kind']}] {x['shown']}" for i, x in enumerate(cands, 1))
    key = hashlib.sha1(f"{prof.get('model')}|{system}|{prompt}".encode("utf-8", "replace")).hexdigest()  # model, prompt, rows
    if isinstance(cache.get(key), dict):
        return cache[key]
    r = models.complete(prof, [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        max_tokens=400, timeout=JUDGE_TIMEOUT, think=False)
    m = re.search(r"\{.*\}", (r or {}).get("text") or "", re.S)
    try:
        j = json.loads(m.group(0)) if m else None
    except ValueError:
        j = None
    if isinstance(j, dict):
        if len(cache) >= CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[key] = j
    return j if isinstance(j, dict) else None


def _verified(j, cands):
    """(row, link2, link3, root) when the pick is in range, each link quotes a phrase held by the picked row and the root
    is known or a new phrase of 3 to 8 words; else None. Nothing the model invents survives."""
    pick = j.get("pick")
    if type(pick) is not int or not 1 <= pick <= len(cands):
        return None
    row = cands[pick - 1]
    hay = " ".join(row["shown"].split()).casefold()
    out = []
    for k in ("link2", "link3"):
        s = j.get(k)
        if not isinstance(s, str) or not 12 <= len(s) <= 400:
            return None
        # a quoted phrase, or the whole link when the model copied the row's words without quote marks
        if not any(" ".join(q.split()).casefold().strip(" .…") in hay for q in _QUOTE.findall(s) + [s]):
            return None
        out.append(c.defang(c.fit(c.plain(s), 300)))
    if out[0] == out[1]:
        out[1] = ""  # one phrase twice is one link: the row's kind supplies the other (_judged_cause)
    root = j.get("root")
    if not isinstance(root, str) or not (root in ROOTS or (3 <= len(root.split()) <= 8 and re.fullmatch(r"[A-Za-z' -]+", root))):
        return None
    return row, out[0], out[1], root


def _judged_cause(row, l2, l3, root, ts, known, ctx):
    """The cause tuple of a verified pick; the cause and catcher follow the row's kind, never the model's say-so."""
    ev = f'{row["id"]} {row["status"] or row["kind"]}: "{c.fit(c.plain(row["text"]), 90)}" (judged from the row)'
    kind = row["kind"]
    l3 = l3 or {"verb": f"why not: which was not run on this ask, so {row['id']} stayed unused",
                "skill": f"why not: which was not run on this ask, so {row['id']} stayed unused",
                "event": f"why not: no read of '{row['id']}' events was set against the ask's words",
                "research": f"why not: {row['id']} was never recalled against the ask"}.get(
                    kind, f"why not: nothing read {row['id']} against the ask")
    if kind == "event":
        got = ctx["consumers"].get(row["id"])
        cat = f"verb:{got[0]}" if got and f"verb:{got[0]}" in ctx["have"] else "hook:user_prompt_submit"
        return "signal", ev, l2, l3, root, cat
    if kind in ("verb", "skill"):
        return "built", ev, l2, l3, root, "verb:which"
    if kind == "research":
        return "known", ev, l2, l3, root, "verb:recall"
    return "known", ev, l2, l3, root, "verb:outcomes" if row["status"] in CLOSED_STATUS else "verb:next"


def _record(ask, cls, ts, known, signal, ctx, judged=None):
    cause, ev, l2, l3, root, catcher = judged or _cause(ask, ts, known, signal, ctx)
    if cause == "blind-spot":
        ev += "; the lens that would have asked: " + LENS.get(cls, LENS["other"])
    r = {"text": ask, "cls": cls, "ts": ts, "cause": cause, "evidence": ev, "catcher": catcher, "root": root,
         "path": "judged" if judged else "rules",
         "detector": f"{catcher}: {DETECTOR[cause]}",
         "chain": [f"evidence: {ev}", l2, f"{l3} ({catcher} should have read it)", f"root: {root}"]}
    if not valid_chain(r, ctx["have"]):  # an unknown catcher: rejected, not shown as if it held
        r["rejected"] = True
    return r


def _remote(days, failed):
    """Every remote project's records (conductor remote runs its own `misses --why --local --json`); an unreachable
    one is named in `failed`. Its text is data: fields are cut and each chain re-checked here."""
    import json
    import subprocess
    from sampo import bus
    out, have = [], catchers()
    for name, r in bus.remotes().items():
        try:
            res = bus._ssh(r, f"{r.get('fm') or bus.REMOTE_FM} misses --why --local --json --no-capture --days {int(days)}")
            if res.returncode:
                raise OSError("exit")
            rows = json.loads(res.stdout).get("records") or []
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            failed.append(name)
            continue
        for x in rows[:200] if isinstance(rows, list) else []:
            if not isinstance(x, dict) or not isinstance(x.get("chain"), list):
                continue
            keys = ("text", "cls", "ts", "cause", "evidence", "catcher", "root", "detector")
            if not all(isinstance(x.get(k), str) for k in keys):  # a list or number is dropped, never hashed or joined
                continue
            x = {k: c.fit(c.plain(x[k]), 300) for k in keys} | {"chain": [c.fit(c.plain(s), 300) for s in x["chain"][:6]
                                                                           if isinstance(s, str)]}
            if x["cause"] in DETECTOR and x["root"] in ROOTS and valid_chain(x, have):
                x["detector"] = f"{x['catcher']}: {DETECTOR[x['cause']]}"  # ours, not the remote's string
                x = {k: c.defang(v) if isinstance(v, str) else [c.defang(s) for s in v] for k, v in x.items()}
                out.append(dict(x, remote=name))
    return out


class _Judge:
    """The local judge over one run: model, pool, document frequencies and the cache. No model up, a call that fails or
    a sensitive project without a loopback model means cause() answers None and the rules decide."""

    def __init__(self, p, known, signal, found):
        from sampo import models, moonscale
        self.models, self.path, self.cache, self.dead = models, os.path.join(p.dir, "misses_why_cache.json"), {}, False
        self.prof, self.vec, self.emb, self.recall, self.cos = None, {}, None, "stem", None
        with contextlib.suppress(Exception):
            self.prof = moonscale._local(bool(c.read_meta(p).get("sensitive")))
        if self.prof:
            import json
            with contextlib.suppress(OSError, ValueError):
                with open(self.path, encoding="utf-8") as f:
                    got = json.load(f)
                got = got if isinstance(got, dict) else {}
                self.cache = got.get("judge") if isinstance(got.get("judge"), dict) else {}
                self.vec = got.get("vec") if isinstance(got.get("vec"), dict) else {}
            self.pool = _pool(known, signal, found)
            self.df = {}
            for r in self.pool:
                for k in r["keys"]:
                    self.df[k] = self.df.get(k, 0) + 1
            with contextlib.suppress(Exception):  # no embedder reachable (or none loopback when sensitive): stem-only
                self.emb = moonscale._embedder(p)
            self.mat = None
        self.dirty = False

    def _vecs(self, texts):
        """Unit vectors for the texts (cached by model and text hash, at most VEC_MAX kept), or None when embedding fails."""
        import base64
        import hashlib
        import math
        import struct
        from sampo import moonscale
        model = self.emb.get("embed_model") or self.emb.get("model") or ""
        keys = [f"{model}|{hashlib.sha1(t.encode('utf-8', 'replace')).hexdigest()[:20]}" for t in texts]
        need = {k: t for k, t in zip(keys, texts) if not isinstance(self.vec.get(k), str)}  # a foreign entry is re-embedded
        if need:
            try:
                got = moonscale._embed(self.emb, need)
            except Exception:  # a down embedder: stem-only for the rest of the run
                self.emb = None
                return None
            for k, v in got.items():  # one signed byte per dimension, base64: a unit vector's cosine survives it
                m = math.sqrt(sum(x * x for x in v)) or 1.0
                self.vec[k] = base64.b64encode(struct.pack(f"{len(v)}b", *(round(x / m * 127) for x in v))).decode()
            while len(self.vec) > VEC_MAX + len(keys):  # the oldest go; this run's own are never evicted
                self.vec.pop(next(iter(self.vec)))
            self.dirty = True
        out = []
        for k in keys:
            s = self.vec.get(k)
            try:
                raw = base64.b64decode(s)
            except (TypeError, ValueError):
                return None
            v = struct.unpack(f"{len(raw)}b", raw)
            m = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / m for x in v])
        return out

    def _sims(self, ask):
        """The ask's cosine to every pool row, or None (stem-only)."""
        if not self.emb:
            return None
        if self.mat is None:
            self.mat = self._vecs([r["shown"] for r in self.pool])
        v = self._vecs([ask[:600]]) if self.mat else None
        if not v:
            self.emb = None
            return None
        self.recall = "stem+embed"
        return [sum(x * y for x, y in zip(v[0], row)) for row in self.mat]

    def cause(self, ask, ts, known, ctx):
        if not self.prof or self.dead:
            return None
        sims = self._sims(ask)
        cands, cos = _retrieve(ask, ts, self.pool, self.df, sims)
        if not cands:
            return None
        n = len(self.cache)
        try:
            j = _judge(self.prof, ask, cands, self.cache, self.models)
        except Exception:  # a down server: one try, then the rules for the rest of the run, not 60 s per ask
            self.dead = True
            return None
        self.dirty = self.dirty or len(self.cache) != n
        if not j:
            return None
        if j.get("pick") in (None, "none", 0):  # the judge found no catcher: a blind spot, said so
            return self._none(cands, cos, sims)
        v = _verified(j, cands)
        if not v:
            return None
        i = cands.index(v[0])
        a = _keys(ask) - {w[:4] for w in FILLER}
        if sims:
            self.cos = cos[i]
            if cos[i] < COS_MIN:  # the pick is not about the ask: no catcher existed
                return self._none(cands, cos, sims)
        elif len(a & v[0]["keys"]) < min(2, len(a)):  # no embedder: a pick must share two content words with the ask
            return None
        return _judged_cause(*v, ts, known, ctx)

    def _none(self, cands, cos, sims):
        near = ", ".join(f"{x['id']}" for x in cands[:3])
        self.cos = max(cos) if cos else None
        return ("blind-spot", f"no catcher existed: the judge read the {len(cands)} nearest rows ({near}) and none addresses the ask",
                f"gap: the nearest rows are about something else ({self.recall} recall)",
                "why not: no scout lens or brainstorm question covers this ground", ROOTS[2], "night:scout")

    def save(self):
        if self.dirty:
            import json
            with contextlib.suppress(OSError):
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"judge": self.cache, "vec": self.vec}, f)
                os.replace(tmp, self.path)


def why(p, days=30, capture=False, remote=True, judge=False):
    """{records, roots: {root: n}, detectors: {root: {task, misses}}, trend: {day: {cause: n, catch: n}}, remotes_failed}:
    the why of the last 30 misses in `days`, plus every remote's, clustered by root; capture files one detector each."""
    from datetime import timedelta
    since = (c.clock() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    asks = [(e.get("data") or {}, str(e.get("ts") or "")) for e in c.ledger_events(p, ["miss"], since=since)]
    seen = {str((d or {}).get("text") or "") for d, _ in asks}
    for e in c.ledger_events(p, ["prompt"], since=since):  # asks the hook logged as prompts before a pattern knew them
        text = c.fit(c.redact(c.plain((e.get("data") or {}).get("text") or "")), MAX_TEXT)
        cls = classify(text)
        if cls and text not in seen:
            seen.add(text)
            asks.append(({"cls": cls, "text": text}, str(e.get("ts") or "")))
    asks = sorted(asks, key=lambda x: x[1])[-30:]
    recs, failed = [], []
    if asks:
        known, signal = _corpus(p, (c.clock() - timedelta(days=days + 14)).strftime("%Y-%m-%dT00:00:00Z"))
        from sampo import which
        found = {"skills": which.skills(), "playbooks": [], "commands": which.commands()}  # loaded once for every ask
        import math
        df = {}
        for r in known + signal:
            for w in r["words"]:
                df[w] = df.get(w, 0) + 1
        n = len(known) + len(signal)
        ctx = {"have": catchers(), "consumers": _consumers(), "rank": lambda t: which.rank(t, found),
               "idf": lambda w: math.log(1 + n / max(1, df.get(w, 0)))}  # an ask word no row holds weighs the most
        judge = _Judge(p, known, signal, found) if judge else None
        recs = []
        for d, ts in asks:
            text = str(d.get("text") or "")
            if judge:
                judge.cos = None
            got = judge.cause(text, ts, known, ctx) if judge else None
            if got and got[0] == "blind-spot" and _cause(text, ts, known, signal, ctx)[0] == "built":
                got = None  # an abstain never overrides which's own match of a verb or skill that already does it
            recs.append(_record(text, d.get("cls") if d.get("cls") in CLASSES else "other", ts, known, signal, ctx, got))
            if got:  # how the evidence was found: stem rank alone or fused with the embedding rank, and the pick's cosine
                recs[-1]["recall"] = judge.recall
                recs[-1]["cos"] = None if judge.cos is None else round(judge.cos, 3)
        if judge:
            judge.save()
    if remote:
        recs += _remote(days, failed)
    good = [r for r in recs if not r.get("rejected")]
    roots, trend = {}, {}
    for r in good:
        roots[r["root"]] = roots.get(r["root"], 0) + 1
        day = trend.setdefault(str(r["ts"])[:10], {})
        day[r["cause"]] = day.get(r["cause"], 0) + 1
    for e in c.ledger_events(p, ["predicted_catch"], since=since):
        day = trend.setdefault(str(e.get("ts") or "")[:10], {})
        day["catch"] = day.get("catch", 0) + 1
    detectors = {}
    if capture and not c.read_meta(p).get("sensitive"):
        from sampo import eco
        for root in list(roots)[:4]:  # at most four filings a run
            mine = [r for r in good if r["root"] == root]
            key = f"why-root: {root} ::"  # the delimiter keeps "a root" from matching "a root and more"
            body = (f"{key}\n{len(mine)} misses reach this root. Build ONE check, tripwire, rule or which-hint "
                    "that would have caught them before the user asked:\n"
                    + "\n".join(f"- {c.fit(c.defang(r['text']), 100)} ({r['cause']}; fix: {r['detector']})"
                                for r in mine[:6]))
            try:
                b, _ = eco.capture_once(p, c.redact(body), "FEATURE", "discovered", key=key, trigger="why-root")
                if key in b.section("Raw request"):  # a brief joined by a mute or budget is not this root's detector
                    detectors[root] = {"task": b.id, "misses": [r["text"] for r in mine]}
            except Exception:
                pass
    return {"records": recs, "roots": roots, "detectors": detectors, "trend": dict(sorted(trend.items())),
            "remotes_failed": failed}
