"""T-5897: the user's own prompt words to one guarded verb. The guard keeps some commands the user's to run (a trigger
budget, a risk switch): an agent that runs one is refused, and a user who asked for it in plain words should not have to
type a command themselves. The prompt hook sees the user's words verbatim, so a Verb here pairs a matcher over those
words with the one guarded call it stands for; `apply` is the hook's single entry.

A grant is one use and short: its key (verb, arguments, the words) is spent by the ledger row `user_grant` that names
the prompt, and the same words again inside EXPIRY_S are the same grant (a prompt delivered twice), never a second use.
Words inside a quote, a code span, a block quote or a paste are somebody's text, not the user's ask: they match nothing
(unquoted). Only a short, plain sentence counts (MAX_WORDS), never a question, a conditional or a negation. The hook
calls apply only for a person's own prompt (not a program's, an agent's, nor another session's text).

A new verb: write a find(clean_text) -> args dict or None and a run(p, args) -> one line, add a Verb to VERBS, and a test
beside test_userwords. Nothing else decides who may call it: the call site (the hook) does, once, for every verb."""
import datetime
import hashlib
import json
import re
from typing import Callable, NamedTuple

from sampo import core as c

EXPIRY_S = 120   # the same words inside this are the one grant, spent already
MAX_WORDS = 30   # a grant is a short sentence; a long request that happens to hold the phrase is a request


class Verb(NamedTuple):
    name: str
    find: Callable   # (clean text) -> an args dict, or None when these words don't ask for it
    run: Callable    # (project, args) -> the one line to tell the user


# ---------------------------------------------------------------- the user's words, minus what is quoted

_QUOTED = (re.compile(r"```.*?(?:```|\Z)", re.S), re.compile(r"`[^`\n]*`"), re.compile(r"<pasted_content[^>]*>.*?(?:</pasted_content[^>]*>|\Z)", re.S),
           re.compile(r"<(code|pre)\b[^>]*>.*?(?:</\1\s*>|\Z)", re.S | re.I),
           re.compile(r"(?m)^(?: {4}|\t).*$"), re.compile(r"(?m)^[ \t]*>.*$"), re.compile(r"\"[^\"\n]*\""), re.compile(r"“[^”\n]*”"),
           re.compile(r"‘[^’\n]*’"), re.compile(r"«[^»\n]*»"), re.compile(r"「[^」\n]*」"),
           re.compile(r"(?<!\w)'[^'\n]{3,}'(?!\w)"))
# what is left of a quote after the pairs are cut is an unbalanced one: the span it opened is somebody's text we can't bound
_STRAY = re.compile(r"[\"“”`«»「」‘]|(?<!\w)'(?=\w)|<(?:code|pre)\b|<pasted_content", re.I)
_OPENERS = frozenset("should what why how is are does did whether if when which who can could would will do shall may".split())
# a block Claude Code or Sampo put in the turn, anywhere in it: the text around it is not the user's (core.MACHINE_TAGS)
_MACHINE_ANY = re.compile(r"<\s*(?:" + "|".join(re.escape(t) for t in c.MACHINE_TAGS) + r"|(?:local-)?command-[a-z-]+)(?=[\s>/])", re.I)


def unquoted(text):
    """The text with every quoted, code, block-quoted and pasted span cut out: what the user said in their own voice."""
    text = str(text or "")
    for rx in _QUOTED:
        text = rx.sub(" ", text)
    return " ".join(text.split())


def _sentence(clean):
    """The words to match: None for an empty, long, questioning or conditional message, or one with an unbalanced quote."""
    words = clean.split()
    if not words or len(words) > MAX_WORDS or "?" in clean or _STRAY.search(clean) or words[0].lower().strip(",.") in _OPENERS:
        return None
    return clean


# ---------------------------------------------------------------- verb: a trigger's daily budget (T-5897)

_ALL = ("discovered", "followup", "self")
# Tight (T-5897 round 2): an explicit verb, the subject ending at a word boundary (not self-review), the value within a
# few small words of it, and the sentence ending there: "make sure follow-ups have no limit on retries" asks nothing.
_SUBJECT = (r"(?<![\w-])(?:(?P<self>(?:self|auto)[- ]?(?:filed|captured|created)|self)|(?P<disc>discovered)|(?P<fol>follow-?ups?))(?![\w-])")
_THING = r"(?:\s+(?:tasks?|items?|captures?|findings?|filings?)(?![\w-]))?"
_VALUE = r"(?:(?P<unl>unlimited)|no\s+(?:limit|cap|budget)|(?:without|with\s+no)\s+(?:a\s+)?(?:limit|cap)|(?P<n>\d{1,4})\s*(?:a|per|/|each)\s*day)\b"
_VERB = r"\b(?:set|make|let|allow|give|lift|remove|raise|limit|cap)\b(?!\s+sure\b)"
_NEAR = r"(?:\s+(?:to|at|for|of|be|have|has|file|get|go|with|a|the)){0,3}\s+"   # the small words between subject and value
_WORDS = r"(?:\s+[\w'-]+){0,%d}?\s+"
_TAIL = r"(?=\s*(?:[.,!;:]|$)|\s+(?:please|now|today|thanks)\b)"
_BUDGET = (re.compile(rf"{_VERB}{_WORDS % 6}{_SUBJECT}{_THING}{_NEAR}{_VALUE}{_TAIL}", re.I),
           re.compile(rf"{_VERB}{_WORDS % 3}{_VALUE}{_NEAR}{_SUBJECT}{_THING}{_TAIL}", re.I))
_NEGATED = re.compile(r"\b(?:don'?t|do\s+not|never|cannot|can'?t|won'?t|shouldn'?t|not)\b", re.I)


def find_budget(clean):
    """{"triggers": [...], "budget": "unlimited" or N} for a plain request to lift or set the daily budget of the
    self-filed (discovered, followup, self), discovered or follow-up tasks, else None."""
    if _NEGATED.search(clean):  # a negation anywhere in the sentence: not a plain ask
        return None
    for rx in _BUDGET:
        m = rx.search(clean)
        if not m:
            continue
        n = int(m.group("n")) if m.group("n") else None
        if n == 0:
            continue
        names = _ALL if m.group("self") else ("discovered",) if m.group("disc") else ("followup",)
        return {"triggers": list(names), "budget": n if n else "unlimited"}
    return None


def run_budget(p, args):
    from sampo import triggers
    for name in args["triggers"]:
        triggers.set_row(p, name, budget=args["budget"])
    what = "an unlimited budget" if args["budget"] == triggers.UNLIMITED else f"a budget of {args['budget']} a day"
    return f"Trigger budget set from your words: {', '.join(args['triggers'])} now have {what}."


# ---------------------------------------------------------------- verb: keep main pushed (T-5938)

# "keep pushing to main", "keep main pushed", "make sure to keep pushing main": a plain, whole-sentence ask to ship trunk
# to the remote's main after each train. The sentence is the quote the standing ship stores.
_KEEP_PUSH = (re.compile(rf"\bkeep\s+(?:on\s+)?(?:pushing|shipping)(?:\s+(?:it|trunk|everything|this))?\s+(?:to\s+)?(?:origin(?:/|\s+)|the\s+remote'?s?\s+)?main\b{_TAIL}", re.I),
              re.compile(rf"\bkeep\s+(?:origin(?:/|\s+))?main\s+(?:pushed|shipped|up\s+to\s+date){_TAIL}", re.I))


def find_keep_pushed(clean):
    """{"why": the sentence} for a plain ask to keep main pushed, else None: the one sentence (cut at a . ! or ; followed
    by a space or the end) the ask is in, capped at 300, never the rest of the message."""
    if _NEGATED.search(clean):
        return None
    for rx in _KEEP_PUSH:
        m = rx.search(clean)
        if m:
            ends = [e.end() for e in re.finditer(r"[.!;]+(?=\s|$)", clean)]
            start = max([e for e in ends if e <= m.start()], default=0)
            end = min([e for e in ends if e >= m.end()], default=len(clean))
            return {"on": True, "why": clean[start:end].strip()[:300]}
    return None


def run_keep_pushed(p, args):
    if getattr(p, "lane", None):  # the hook runs in the user's session; a lane's project never turns it on
        return "Standing ship not turned on: this is a builder lane."
    c.set_standing_ship(p, True, args["why"], "the prompt hook")
    return "Keep main pushed, from your words: the standing ship is on, so the land now ships trunk to the remote's main after each train that merged."


VERBS = (Verb("triggers-budget", find_budget, run_budget), Verb("standing-ship", find_keep_pushed, run_keep_pushed))


# ---------------------------------------------------------------- the hook's entry

def find(text):
    """[(verb, args)] the user's own words in text ask for. Nothing for a slash command line (Sampo itself types
    "/compact Keep T-x <title>" into the pane: the title is not the user's) or a prompt holding a machine block anywhere."""
    text = str(text or "")
    if text.lstrip().startswith("/") or _MACHINE_ANY.search(text):
        return []
    clean = _sentence(unquoted(text))
    out = []
    for v in VERBS if clean else ():
        args = v.find(clean)
        if args:
            out.append((v, args))
    return out


def _key(verb, args, clean):
    return hashlib.sha256(json.dumps([verb.name, args, clean.lower()], sort_keys=True).encode()).hexdigest()[:12]


def _turn(clean):
    return hashlib.sha256(clean.lower().encode()).hexdigest()[:12]


def apply(p, text, session=None, now=None):
    """Run each verb the user's words ask for, once: the lines to tell the user. Each grant is a `user_grant` ledger row
    (the verb, its arguments, the prompt) written before the verb runs, so a run that fails still spent it. A prompt
    delivered twice is one grant: the words are skipped only while their grant is the newest row inside EXPIRY_S (an
    "unlimited", a "5 a day" and the "unlimited" again are three grants), and the skip says so. Never raises for a
    verb's failure: that says so in its line."""
    found = find(text)
    if not found:
        return []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = (now - datetime.timedelta(seconds=EXPIRY_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clean = unquoted(text)
    sid = session or c.session_id()
    # only this session's own prompt grants count: a row another writer posted (via, session) is not a spent grant
    rows = [e.get("data") or {} for e in c.ledger_events(p, ["user_grant"], since=since)
            if e.get("session_id") == sid and (e.get("data") or {}).get("via") == "prompt"]
    turn = _turn(clean)
    spent = {r.get("key") for r in rows if r.get("turn") == turn} if rows and rows[-1].get("turn") == turn else set()  # the newest words
    sensitive = bool(c.read_meta(p).get("sensitive"))
    lines = []
    for verb, args in found:
        key = _key(verb, args, clean)
        if key in spent:
            lines.append(f"Sampo: an identical grant is already on the ledger ({verb.name}, a moment ago); nothing was run again.")
            continue
        data = {"verb": verb.name, "args": args, "key": key, "turn": _turn(clean), "via": "prompt", "expires_s": EXPIRY_S}
        if not sensitive:
            data["prompt"] = c.fit(c.redact(c.plain(text)), 300)
        c.log_event(p, "user_grant", data=data, session=session)
        try:
            lines.append(verb.run(p, args) + " (Done by Sampo's prompt hook, not the model.)")
        except Exception as e:  # the grant is spent and logged: say it didn't run rather than let it look done
            lines.append(f"Sampo: {verb.name} did not run ({type(e).__name__}); in a terminal: sampo triggers set NAME --budget N|unlimited.")
    return lines
