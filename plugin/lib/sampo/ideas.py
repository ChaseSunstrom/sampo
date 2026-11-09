"""fm ideas: tool-less brainstorm children, one per lens, run in parallel (skills/brainstorm).

A Claude Code subagent can't have zero tools (an empty `tools:` list means every tool), so each lens runs as a
fresh `claude -p` session with no built-in tools and no MCP servers, in a scratch directory outside any project.
"""
import glob
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter

from sampo import budget
from sampo import core as c
from sampo import recall

LENSES = ["user value", "unspoken needs", "delight", "capability map", "approaches", "reliability", "performance",
          "security and safety", "simplicity", "bold bets", "beautiful UI and motion", "every device and surface",
          "agents of agents", "privacy and local-first", "reframe", "flip assumptions", "oblique provocation",
          "devil's idea", "worst-bugs persona"]
# T-0627/T-0628: what a lens whose name isn't enough asks for
LENS_NOTES = {
    "reframe": "restate the problem three different ways (as the user's goal, as a constraint to remove, as a question "
               "nobody asked) and give the ideas each framing opens",
    "flip assumptions": "list the assumptions the request rests on, flip each one, and give the ideas that only exist "
                        "once it's false",
    "oblique provocation": "take one past lesson or failure from the pack as a provocation and follow it somewhere "
                           "unexpected",
    "devil's idea": "propose what a rival who wanted this project to win would ship that its owner would never dare to",
    "worst-bugs persona": "be the user who hits the worst bugs: what breaks, confuses or loses work, and what would "
                          "have prevented it",
    # T-2672: exhaustive requests: one idea per cell of surfaces x actors x failure modes
    "grid": "list the request's surfaces (from the pack's surfaces line, or the project's entry points) and its actors "
            "(the user, the agent, other agents, an unattended run), cross them with failure modes (missing, wrong, "
            "slow, unsafe, unclear), and give one idea per cell that matters, naming the cell; say which cells you "
            "left empty and why",
    # T-2076: added when earlier brainstorms missed asks the user then had to make
    "blind spots": "the pack lists asks the user had to make that no earlier brainstorm predicted: for each, name the "
                   "question a brainstorm should have asked to catch it, ask that question of the whole project now, and "
                   "give the ideas it surfaces (what else in that class is still missing)",
}
# T-0605: the capability axes a complete catalogue covers; thin ones are named for the next round
AXES = ["user value", "reliability", "performance", "security", "privacy", "ui", "devices", "agents", "simplicity",
        "developer experience", "observability", "docs", "delight"]
PACK_WORDS = 2000
# T-0099: unspoken needs and delight by default (the user wanted what they can't put into words, and more creative ideas)
# T-0375: fm mission adds the UI, device, agent and privacy lenses when the project has those surfaces
# T-0364: capability map (everything a complete version has) and approaches (every way to solve it): breadth
DEFAULT_LENSES = ["user value", "unspoken needs", "delight", "capability map", "approaches", "reliability", "simplicity",
                  "bold bets"]
PROMPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(__file__)))),
                      "skills", "brainstorm", "references", "ideas-prompt.md")
# No built-in tools, no MCP servers, and no user settings (so other plugins' hooks and prompts don't bias the lens).
NO_TOOLS = ["--setting-sources", "project,local", "--tools", "", "--strict-mcp-config",
            "--mcp-config", json.dumps({"mcpServers": {}})]


WARM = 10  # T-1265: seconds the first lens runs alone, so the rest read the pack from the prompt cache it wrote


_CANARY = re.compile(r"canary:\s*(.+?)\s+found\b")


def canary_spread(text):
    """T-2686: (term, count, total) when one canary term covers over half of a catalogue's 10+ canary lines, else
    None. Such a term ('def main') shows the search ran, not that the idea's phrasing could find a built neighbour."""
    terms = [m.group(1).strip("`'\" ") for m in _CANARY.finditer(text)]
    top = Counter(terms).most_common(1)
    return (top[0][0], top[0][1], len(terms)) if len(terms) >= 10 and top[0][1] * 2 > len(terms) else None


def child_cmd(model, system, system_file=None):
    """The prompt goes in on stdin and a big system text in a file (system_file): packs can exceed the per-argument
    size limit."""
    c.refuse_if_paused()  # T-0591: fm relate's child comes through here too
    return ["claude", "-p", "--model", model, "--no-session-persistence", "--output-format", "json", *NO_TOOLS,
            "--max-budget-usd", f"{budget.effective()[0]['run']:g}",  # T-0776: a hard stop at the per-command cap
            *(["--append-system-prompt-file", system_file] if system_file else ["--append-system-prompt", system])]


def _cache_tokens(so):
    """(read, written) prompt-cache tokens from claude -p's json; (0, 0) when it says nothing."""
    try:
        u = json.loads(so).get("usage") or {}
        return int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
    except (ValueError, TypeError, AttributeError):
        return 0, 0


def _tail(s, n):
    """The end of a child's output (claude puts its error last), on one safe line."""
    s = c.plain(" ".join((s or "").split()))
    return s if len(s) <= n else "…" + s[1 - n:]


def _child_dir():
    """T-0856: the one private folder every tool-less child runs in. Claude Code makes a ~/.claude/projects folder
    per working directory, so a fresh temp dir per child left 564 of them. OSError when it isn't ours to use."""
    import stat
    d = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), f"fm-children-{os.getuid()}")
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)  # never a link or someone else's folder (a shared /tmp)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise OSError(f"{d} isn't a private folder of ours")
    return d


def _child_cwd(feature):
    import contextlib
    try:
        return contextlib.nullcontext(_child_dir())
    except OSError:
        return tempfile.TemporaryDirectory(prefix=f"fm-{feature}-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)


def _child_env():
    return dict(os.environ, FOREMAN_NO_BACKGROUND="1")  # a child never starts Foreman's own background reviews


def _registry(pid=""):
    return os.path.join(c.state_dir(), "children", str(pid))


PROC = "/proc"  # T-1249: tests point it at a missing folder to run the ps path macOS takes


def _ps(pid, field):
    """ps's FIELD for a live pid, where there's no /proc (macOS, BSD); FileNotFoundError when it's gone, as /proc's."""
    try:
        out = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    if not out:
        raise FileNotFoundError(f"no process {pid}")
    return out


def _starttime(pid):
    """When the pid started: with the pid, one process ever. Clock ticks since boot (/proc/<pid>/stat field 22), or
    where there's no /proc, ps's start time with its spaces closed up (the registry line splits on them)."""
    if os.path.isdir(PROC):
        with open(f"{PROC}/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    return "".join(_ps(pid, "lstart").split())


def _cmdline(pid):
    """A live pid's arguments as bytes (/proc/<pid>/cmdline, else ps: split on spaces, enough to find a flag)."""
    if os.path.isdir(PROC):
        with open(f"{PROC}/{pid}/cmdline", "rb") as f:
            return f.read().split(b"\0")
    return os.fsencode(_ps(pid, "command")).split()  # after the name, which may hold spaces and parentheses


def _popen(argv, timeout, **kw):
    """T-1044: a child in its own process group, so a timeout kills its grandchildren too, registered in
    state/children/<pid> (its start time, when, its timeout) so a SessionStart sweeps it if this run dies first."""
    pr = subprocess.Popen(argv, start_new_session=True, **kw)
    try:
        c.write_atomic(_registry(pr.pid), f"{_starttime(pr.pid)} {time.time():.0f} {timeout:.0f}")
    except (OSError, IndexError):  # no state folder, or ps couldn't see it: unregistered, still reaped on a timeout
        pass
    return pr


def _forget(pr):
    try:
        os.remove(_registry(pr.pid))
    except OSError:
        pass


def _reap(pr):
    """T-1044: the child's whole process group killed (never only the leader: a grandchild holding the pipes hung the
    runner and outlived it), its pipes drained, its registry entry gone."""
    if pr.returncode is None:  # once it's been waited for, its pid may be another process's by now
        c.kill_group(pr)
    try:
        pr.communicate(timeout=5)
    except subprocess.TimeoutExpired:  # a grandchild that left the group (setsid) holds the pipes: not ours to wait on
        pr.wait()
    _forget(pr)


def _strays(pgid, start):
    """T-1044: a dead leader's group still has a member it started: a process in the session and group the leader made
    (start_new_session: both ids are its pid), started no earlier than it. Linux never reuses a pid while a group holds
    it; only a later session leader that took the pid could make such a group, and a sweep can't tell that one apart."""
    try:
        os.killpg(pgid, 0)  # no such group (the usual case: the helper finished) costs one call, not a /proc scan (~30 ms)
    except OSError:
        return False
    if not os.path.isdir(PROC):  # T-1249: no /proc to read members' start times; a live group still holding the dead
        return True  # leader's pid as its id is the one it made (POSIX doesn't reuse a pid a group still holds)
    for d in os.listdir(PROC):
        if d.isdigit():
            try:
                with open(f"{PROC}/{d}/stat") as f:
                    st = f.read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if st[2] == st[3] == str(pgid) and int(st[19]) >= int(start):
                return True
    return False


def sweep_orphans(max_age=1800):
    """T-1044: the helpers a dead run left behind, killed with their process groups: the count. An entry older than
    max_age (or its own timeout) whose pid is still the same process (its start time) running a helper (its command
    line) is killed, and so is the group of a leader that's gone while a member it started holds the group (_strays);
    a reused or foreign pid, or an empty group, only loses its entry; a fresh one is left to its run."""
    try:
        names = os.listdir(_registry())
    except OSError:
        return 0
    n = 0
    for name in names:
        if name.startswith("."):  # a write_atomic temp file mid-write
            continue
        path = _registry(name)
        try:
            with open(path) as f:
                start, at, timeout = f.read().split()
            if time.time() - float(at) < max(max_age, float(timeout) + 60):
                continue  # its run may still be waiting on it
            pid = int(name)
            try:
                ours = b"--no-session-persistence" in _cmdline(pid) and _starttime(pid) == start
            except FileNotFoundError:  # the leader is gone
                ours = _strays(pid, start)  # T-1249: ps's start time is no number; only the /proc scan compares it
            if ours:
                os.killpg(pid, signal.SIGKILL)
                n += 1
        except (OSError, ValueError, IndexError):  # gone, garbled or not ours to kill: the entry goes
            pass
        try:
            os.remove(path)
        except OSError:
            pass
    return n


def run_child(feature, system, prompt, model, timeout, project=None, detail="", fallback=0.05):
    """T-0295: one tool-less child, budget-checked, run in a scratch folder and recorded: its text. ValueError when
    it can't run, fails or says nothing."""
    try:
        budget.check(feature, budget.estimate(feature, 1, fallback))
    except budget.BudgetError as e:
        raise ValueError(str(e))
    with _child_cwd(feature) as cwd:
        try:
            pr = _popen(child_cmd(model, system), timeout, cwd=cwd, text=True, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env())
        except OSError as e:
            raise ValueError(f"the {feature} child didn't run: {e} (is `claude` on PATH and logged in?)")
        try:
            so, se = pr.communicate(prompt, timeout=timeout)
        except BaseException as e:  # a timeout or ^C: the whole group goes (subprocess.run killed only the leader)
            _reap(pr)
            if isinstance(e, subprocess.TimeoutExpired):  # never str(e): it holds the argv, the whole system prompt
                raise ValueError(f"the {feature} child timed out after {timeout} s (is `claude` logged in?)")
            raise
        _forget(pr)
    text, usd = budget.result(so)
    budget.record(feature, usd, project=project, detail=detail)
    if pr.returncode or not (text or "").strip():
        raise ValueError(f"nothing came back (exit {pr.returncode}: {_tail(se or so, 160)})")
    return text


def run_children(jobs, timeout, feature, cwd=None, warm=0, cache=None):
    """T-0295: [(argv, stdin, detail)] run in parallel in one scratch folder under one deadline, each recorded:
    [(text or None, error)]; both set is a partial answer (T-0959). OSError when one can't start (the started ones are
    stopped). cwd: run there instead (T-1186: reviewers read the project). warm (T-1265): the first runs alone until it
    ends or `warm` s pass, so what the jobs share is in the prompt cache for the rest; cache: a dict the prompt-cache
    tokens read and written are added to."""
    import contextlib
    with (contextlib.nullcontext(cwd) if cwd else _child_cwd(feature)) as cwd:
        procs, started = [], False
        try:
            for i, (argv, stdin, detail) in enumerate(jobs):
                if i == 1 and warm > 0:
                    try:
                        procs[0][0].wait(timeout=warm)
                    except subprocess.TimeoutExpired:
                        pass
                pr = _popen(argv, timeout, cwd=cwd, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_child_env())
                procs.append((pr, detail))
                try:
                    pr.stdin.write(stdin)
                    pr.stdin.close()
                finally:
                    pr.stdin = None  # before 3.13, communicate() flushes a closed stdin and raises
            started, deadline, out = True, time.time() + timeout, []
            for pr, detail in procs:
                try:
                    so, se = pr.communicate(timeout=max(1, deadline - time.time()))
                    _forget(pr)
                    text, usd = budget.result(so)  # T-0227: the text, and what it cost
                    budget.record(feature, usd, detail=detail)
                    if cache is not None:
                        read, written = _cache_tokens(so)
                        cache["read"], cache["written"] = cache.get("read", 0) + read, cache.get("written", 0) + written
                    why = f"exit {pr.returncode}: {_tail(se or so, 200)}"
                    # T-0959: text and an error is a partial answer (it printed, then failed): kept, flagged
                    out.append((text, None) if pr.returncode == 0 and text.strip() else
                               (text, why) if text.strip() else (None, why))  # both set: partial
                except subprocess.TimeoutExpired:
                    _reap(pr)
                    budget.record(feature, None, detail=f"{detail} (timed out: cost unknown)" if detail
                                    else "timed out: cost unknown")
                    out.append((None, f"timed out after {timeout}s"))
        except BaseException as e:  # T-1044: a child that can't start, or ^C: every group still running goes (each has
            for pr, _ in procs:  # its own session, so the terminal's ^C no longer reaches them)
                if pr.returncode is None:
                    _reap(pr)
            if isinstance(e, OSError) and not started:
                raise OSError(f"can't start claude: {e} (is it on PATH and logged in?)")
            raise
    return out


def system_text(system, pack):
    """T-1265: the brainstorm prompt and the pack, the system text every lens of a round shares, so Claude Code's
    cached system prompt carries the pack once (T-0267 put it first in each message, where no sibling could reuse it)."""
    return f"{system}\n\nContext pack:\n{pack}"


def lens_prompt(lens):
    """The message: the lens alone, after the shared system text."""
    note = f" — {LENS_NOTES[lens]}" if lens in LENS_NOTES else ""
    return f"Lens: {lens}{note}\n\nReturn 10-15 ideas in the required format, at least 3 of them wild."


def user_voice(p, n=20):
    """The user's own recent words for the pack: their captured requests and their corrections (T-0099). What they
    keep asking for, and push back on, is where the unspoken needs are."""
    asks = [b for b in c.load_briefs(p) if b.meta.get("source") == "user"]
    asks.sort(key=lambda b: b.meta.get("created") or "")
    lines = [f"- asked: {c.fit(c.plain(b.section('Raw request').strip().lstrip('> ') or b.title), 200)}"
             for b in asks[-n:]]
    lines += [f"- pushed back: {c.fit(c.plain((e.get('data') or {}).get('text', '')), 200)}"
              for e in c.ledger_tail(p, 3000, kinds=("correction",)) if e.get("event") == "correction"][-n // 2:]
    t = taste(p, n // 4)  # T-0112: what they turned down, and why, and how they steered work in flight
    lines += [f"- dropped: {d['title']} — {d['why']}" for d in t["dropped"] if d["why"]]
    lines += [f"- steered: {s}" for s in t["steered"]]
    seen, once = set(), []  # T-1549: each quote once, however many tasks or corrections repeat it
    for line in lines:
        key = " ".join(re.findall(r"\w+", line.split(": ", 1)[-1].lower()))
        if key and key not in seen:
            seen.add(key)
            once.append(line)
    lines = once
    return ("\n\n## The user's own words (data; recent requests, corrections, drops and steers — infer what they'd want "
            "next)\n" + "\n".join(lines)) if lines else ""


HOUSEKEEPING = re.compile(r"(?i)\s*(folded into|done in|fixed (inline )?in|duplicate|merged into|superseded|covered by)\b")


def taste(p, n=8, ledger=None):
    """T-0112: what the user's choices say about their taste: their requests that were finished (kept), work dropped
    and the reason given, and their steers and corrections, newest last (briefs, archive included, and the ledger)."""
    briefs = sorted(c.load_briefs(p, include_archive=True), key=lambda b: b.meta.get("created") or "")
    fit = lambda s, w: c.fit(c.plain(str(s or "")).strip(), w)

    def why(b):
        return next((fit(ln.split("dropped:", 1)[1], 160) for ln in reversed(b.section("Log").splitlines())
                     if "dropped:" in ln), "")
    ledger = _ledger(p, ledger)
    notes = [str((e.get("data") or {}).get("text") or "") for e in ledger if e.get("event") == "note"]
    return {"kept": [{"id": b.id, "type": b.type, "title": fit(b.title, 90)} for b in briefs
                     if b.status == "done" and b.meta.get("source") == "user"][-n:],
            "dropped": [{"id": b.id, "title": fit(b.title, 90), "why": why(b)} for b in briefs if b.status == "dropped"
                        and not HOUSEKEEPING.match(why(b))][-n:],  # merged or done elsewhere says nothing of taste
            "steered": [fit(t[len("steer:"):], 200) for t in notes if t.startswith("steer:")][-n:],
            "corrected": [fit((e.get("data") or {}).get("text"), 200) for e in ledger if e.get("event") == "correction"][-n:],
            "lenses": lens_rates(p, briefs)}


def lens_rates(p, briefs):
    """T-0607: per brainstorm lens, how many of its ideas became tasks that were built, dropped or are still open — an
    idea matches a brief whose title holds most of its words."""
    rates = {}
    briefs = [(b, _words(b.title)) for b in briefs]
    for path in glob.glob(os.path.join(p.dir, "research", "brainstorm-*", "ideas.json")):
        try:
            with open(path, encoding="utf-8") as f:
                lenses = json.load(f).get("lenses") or {}
        except (OSError, ValueError):
            continue
        for lens, titles in lenses.items():
            r = rates.setdefault(lens, {"ideas": 0, "built": 0, "dropped": 0, "open": 0})
            for t in titles:
                r["ideas"] += 1
                w = _words(t)
                b = next((b for b, bw in briefs if w and len(w & bw) / len(w) >= 0.6), None)
                if b:
                    r["built" if b.status == "done" else "dropped" if b.status == "dropped" else "open"] += 1
    return rates


def _content(title):
    return {w[:5] for w in _WORD.findall(str(title).lower()) if w not in recall._STOP}


def _brainstorms(p):
    """T-2076: every saved brainstorm's ideas.json as (created, data), oldest first; one without a date sorts first."""
    out = []
    for path in glob.glob(os.path.join(p.dir, "research", "brainstorm-*", "ideas.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append((str(data.get("created") or ""), data))
    return sorted(out, key=lambda x: x[0])


def misses(p, n=8):
    """T-2076: the asks the user had to make that no brainstorm predicted, newest last, as (id, title): a request they
    captured after the first brainstorm whose words no earlier idea title shares half of (at least two). Each is a
    question the brainstorms never asked; learned from the data, so no list of areas goes stale."""
    runs = _brainstorms(p)
    if not runs:
        return []
    index, made = {}, []  # content word -> idea numbers; idea number -> its brainstorm's date
    for created, data in runs:
        for titles in (data.get("lenses") or {}).values():
            for t in titles if isinstance(titles, list) else ():
                for w in _content(t):
                    index.setdefault(w, []).append(len(made))
                made.append(created)
    out = []
    for b in sorted(c.load_briefs(p, include_archive=True), key=lambda b: str(b.meta.get("created") or "")):
        when, words = str(b.meta.get("created") or ""), _content(b.title)
        if b.meta.get("source") != "user" or when <= runs[0][0] or not words:
            continue
        hits = Counter(i for w in words for i in index.get(w, ()) if made[i] < when)
        if max(hits.values(), default=0) < max(min(2, len(words)), (len(words) + 1) // 2):
            out.append((b.id, c.fit(c.plain(b.title).strip(), 120)))
    return out[-n:]


def questions(p, n=8):
    """T-2076: the questions earlier brainstorms said the next one should ask, newest last, each once."""
    seen = {}
    for _, data in _brainstorms(p):
        for q in data.get("questions") or []:
            q = c.fit(c.plain(str(q)).strip(), 200)
            seen.pop(q.lower(), None)
            if q:
                seen[q.lower()] = q
    return list(seen.values())[-n:]


# T-0439: a no the user keeps steering with becomes a veto to propose; their other steers and finished requests say
# which option they'd pick
TASTE_REPEATS = 3
_NO = re.compile(r"(?i)\b(?:no|not|never|don['’]?t|do not|stop|avoid|without)\b")
_STEER_STOP = {"keep", "prefer", "instead", "rather", "should", "shouldn", "want", "avoid", "never", "don", "not",
               "stop", "more", "less", "only", "please", "let", "lets", "like", "better", "steer"}


def _key_words(text):
    return list(dict.fromkeys(w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
                              if w not in c._VETO_STOP and w not in _STEER_STOP))


_TYPES = (("safety", "force|delete|secret|push|destroy|rm|reset"), ("scope", "only|also|outside|just|scope"),
          ("style", "format|name|naming|comment|lint|style|camelcase|snake"))


def classify(text):
    """T-1063: what a steer's shape words are about: safety, scope, style, else a wrong approach (whole words, no model)."""
    return next((k for k, rx in _TYPES if re.search(rf"(?i)\b(?:{rx})s?\b", text or "")), "wrong approach")


# a word the steer names as a command: anchored to the command, not to prose that says the word
_TOKENS = {"push": r"\bgit\s+(?:-\S+\s+)*push\b", "commit": r"\bgit\s+(?:-\S+\s+)*commit\b",
           "reset": r"\bgit\s+(?:-\S+\s+)*reset\b", "rebase": r"\bgit\s+(?:-\S+\s+)*rebase\b",
           "force": r"(?:\s--force(?:-with-lease)?\b|\s-f\b)", "rm": r"(?:^|[;&|]\s*)rm\b"}


def _pattern(words):
    return "(?i)^" + "".join(rf"(?=[\s\S]*{_TOKENS.get(x) or rf'(?<![\w-]){re.escape(x)}\b'})" for x in words)


def compile_rule(p, x, block=False):
    """T-1063: the rule a proposal compiles to: a warn on bash (a block only on the user's --block); two shape words at
    least, so one common word never makes a rule."""
    if len(x["words"]) < 2:
        raise ValueError(f"'{' '.join(x['words'])}' is one word: too loose to compile; fm taste adopt makes a veto instead")
    return c.add_rule(p, _pattern(x["words"]), "bash", "block" if block else "warn", f"{x['type']} steer: {x['said']}")


def _stem(w):
    # ponytail: -s/-es/-ing/-ed only ("mocks", "mocking" → "mock", "branches" → "branch"); a real stemmer if clusters miss
    s = re.sub(r"(?:ing|ed|(?:(?<=ss)|(?<=[xz])|(?<=[cs]h))es|(?<![su])s)$", "", w)
    return s if len(s) >= 3 else w


def _covered(p):
    """Stemmed word sets a recorded veto or a declined proposal covers (T-1042: steer words are kept unstemmed, a
    correction cluster's stemmed, so both sides compare as stems)."""
    return [{_stem(w) for w in ws} for ws in [v["words"] for v in c.vetoes(p)] + (c.read_meta(p).get("taste_declined") or [])]


def _ledger(p, ledger):
    return c.ledger_tail(p, 3000) if ledger is None else ledger


def proposals(p, ledger=None):
    """[{words, count, said}]: key words shared by TASTE_REPEATS or more steers that say no, as vetoes to propose; none
    a recorded veto or a declined proposal already covers (its words a subset of the shape)."""
    notes = [str((e.get("data") or {}).get("text") or "") for e in _ledger(p, ledger) if e.get("event") == "note"]
    sets = [(s, _key_words(s)) for s in (t[len("steer:"):].strip() for t in notes if t.startswith("steer:")) if _NO.search(s)]
    covered = _covered(p)
    out, seen = [], set()
    for w in dict.fromkeys(x for _, ws in sets for x in ws):
        group = [(s, ws) for s, ws in sets if w in ws]
        shape = [x for x in group[-1][1] if all(x in ws for _, ws in group)][:3]
        standing = any(c.STANDING_STEER.search(x) for x, _ in group)  # T-0601: said as a rule, once is enough
        if (len(group) < TASTE_REPEATS and not standing) or tuple(sorted(shape)) in seen \
                or any(h <= {_stem(x) for x in shape} for h in covered) \
                or any(r["pattern"] == _pattern(shape) for r in c.rules(p)):  # T-1063: compiled already
            continue
        seen.add(tuple(sorted(shape)))
        said = c.fit(c.plain(group[-1][0]), 160)
        out.append({"words": shape, "count": len(group), "said": said, "type": classify(" ".join(shape))})
    return out


CORRECTION_REPEATS = 2
_CORRECTION_STOP = {"nope", "wrong", "said", "meant", "why", "did", "what", "didn", "doesn", "wasn", "isn", "don", "won",
                    "can", "already", "think", "would", "because"}  # how a correction is said, not what it's about


def correction_clusters(p, ledger=None):
    """T-1042: [{words, count, said, suggest}]: key words (stemmed) shared by CORRECTION_REPEATS or more of the user's
    corrections, each one the user rarely says otherwise (in at most a tenth of their prompts), with a ready fm rule
    add line; none a recorded veto, a rule or a declined proposal already covers."""
    ledger = _ledger(p, ledger)
    stems = lambda t: list(dict.fromkeys(s for s in (_stem(w) for w in _key_words(t) if w not in _CORRECTION_STOP)
                                         if s not in c._VETO_STOP))
    sets = [(t, stems(t)) for t in (str((e.get("data") or {}).get("text") or "") for e in ledger
                                     if e.get("event") == "correction")]
    prompts = [stems(str((e.get("data") or {}).get("text") or "")) for e in ledger if e.get("event") == "prompt"]
    df, covered = Counter(x for ps in prompts for x in ps), _covered(p)
    out, seen = [], set()
    for w in dict.fromkeys(x for _, ws in sets for x in ws):
        group = [(s, ws) for s, ws in sets if w in ws]
        shape = [x for x in group[-1][1] if all(x in ws for _, ws in group)
                 and df[x] <= max(len(group), len(prompts) // 10)][:3]  # a word they say all the time ties nothing
        if len(group) < CORRECTION_REPEATS or not shape or tuple(sorted(shape)) in seen \
                or any(h <= set(shape) for h in covered) \
                or any(c.rule_hits(p, on, " ".join(shape)) for on in ("bash", "path")):
            continue
        seen.add(tuple(sorted(shape)))
        said = c.fit(c.plain(group[-1][0]), 160)
        pattern = "(?i)^" + "".join(rf"(?=[\s\S]*\b{re.escape(x)})" for x in shape)  # as fm rule add --from-veto
        out.append({"words": shape, "count": len(group), "said": said, "type": classify(" ".join(shape)),
                    "suggest": f"fm rule add {shlex.quote(pattern)} --warn --why {shlex.quote(said)}"})
    return out


def norm_ask(q):
    return re.sub(r"[\W_]+", " ", q.lower()).strip()


def ask_key(q, options):
    return norm_ask(q) + "|" + "|".join(sorted(norm_ask(o) for o in options or []))


def precedent(p, q, options=None):
    """T-1064: (answer, n) when at least 2 earlier user answers (--answer is the user's alone: the guard) to this ask
    (same normalized question and options) agree and none differ, the answer is one of the options, and the proxy's
    own record is good; else None. The record: two overruled proxies, or over 20% once 3 exist, stops it."""
    key, answers, proxied, overruled = ask_key(q, options), [], 0, 0
    if not norm_ask(q):
        return None
    kinds = ("ask_answered", "proxy_answer", "proxy_overruled")
    for e in c.ledger_events(p, kinds, since=c.iso(time.time() - 180 * 86400)):
        d = e.get("data") or {}
        if e.get("event") == "ask_answered" and d.get("q") == key:
            answers.append(d.get("a"))
        proxied += e.get("event") == "proxy_answer"
        overruled += e.get("event") == "proxy_overruled"
    if len(answers) < 2 or len(set(answers)) != 1 or (options and answers[0] not in options):
        return None
    if overruled >= 2 or (proxied >= 3 and overruled / proxied > 0.2):
        return None
    return answers[0], len(answers)


def taste_default(p, options):
    """(option, why): the option the user's record favours — none a recorded veto covers (unless every one is), then the
    most key words shared with their finished requests and their steers that aren't a no; a tie keeps the caller's order."""
    vetoed = {o: c.veto_hits(p, o) for o in options}
    free = [o for o in options if not vetoed[o]] or list(options)
    t = taste(p, 50)
    liked = {w for s in [k["title"] for k in t["kept"]] + [s for s in t["steered"] if not _NO.search(s)]
             for w in _key_words(s)}
    best = max(free, key=lambda o: sum(w in liked for w in _key_words(o)))
    why = ["avoids the veto " + "; ".join(f"\"{vetoed[o][0]['said']}\"" for o in options if o not in free)] \
        if len(free) < len(options) else [f"every option hits a veto (\"{vetoed[options[0]][0]['said']}\"): the "
                                          f"least bad by your record"] if options and all(vetoed.values()) else []
    hits = [w for w in _key_words(best) if w in liked]
    why += [f"like your steers and finished requests ({', '.join(hits)})"] if hits else []
    return best, ("taste record: " + "; ".join(why)) if why else "no signal in your taste record: the first option"


OVERWRITE_DAYS = 14


def overwrites(p, days=90):
    """T-0655: [(file, agent task, user's commit subject, sha)]: a commit without a Foreman-Task trailer (the user's)
    that changed a file an agent commit changed within OVERWRITE_DAYS before — taste learned from the rework."""
    log = c._git(p.root, "log", f"--since={days}.days", "--no-renames", "--format=%x01%H%x00%ct%x00%B%x00", "--name-only",
                 timeout=60) or ""
    last, out = {}, []
    for entry in reversed(log.split("\x01")[1:]):
        sha, ts, body, names = (entry.split("\x00") + ["", "", ""])[:4]
        task = (c.task_trailers(body) or [None])[0]
        for f in (x for x in names.split("\n") if x.strip()):
            if task:
                last[f] = (task, int(ts or 0))
            elif f in last and int(ts or 0) - last[f][1] <= OVERWRITE_DAYS * 86400:
                out.append((f, last.pop(f)[0], c.fit(c.plain(body.strip().splitlines()[0] if body.strip() else ""), 100),
                            sha[:10]))
    return out


def cmd_taste(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "overwrites", False):
        rows = overwrites(p)
        return cli.out(args, {"overwrites": [{"file": f, "task": t, "subject": s, "sha": h} for f, t, s, h in rows]},
                         ("Your rework of an agent's change (taste to learn from):\n" + "\n".join(
                             f"  {f} after {t}: {s} ({h})" for f, t, s, h in rows[-args.n * 2:])) if rows else
                         "No commit of yours reworked an agent's file within two weeks of it.")
    ledger = c.ledger_tail(p, 3000)
    props, corr = proposals(p, ledger), correction_clusters(p, ledger)
    if args.action:  # the user's one yes (or no), asked through AskUserQuestion: vetoes only add caution
        # T-1042: a bare adopt or decline takes the steer proposals; a correction cluster only by its N (numbered on)
        pick = props if args.which is None else (props + corr)[args.which - 1:args.which] if args.which > 0 else []
        if args.action == "compile" and not args.which:  # T-1063: one rule, named; never every proposal at once
            raise cli.UsageError("fm taste compile N: name the proposal (fm taste lists them)")
        if args.block and (args.action != "compile" or any("suggest" in x for x in pick)):
            raise cli.UsageError("--block is for fm taste compile N on a steer proposal; a correction compiles to a warn")
        if not pick:
            raise cli.UsageError(f"no proposal {args.which or ''} to {args.action}; fm taste lists them".replace("  ", " ")
                                   + (" (a correction is taken by its number)" if corr and args.which is None else ""))
        if args.action == "compile":  # T-1063: the steer's type decides the rule; the proposal is then covered by it
            try:
                compile_rule(p, pick[0], args.block)
            except ValueError as e:
                raise cli.UsageError(str(e))
            n = len(c.rules(p))
            kind = "block" if args.block else "warn"
            done = f"{kind} rule {n} added (fm rule rm {n} is yours to run)"
        elif args.action == "adopt":
            for x in pick:
                how = "corrected" if "suggest" in x else "steered"
                c.add_veto(p, f"{x['said']} ({how} {x['count']} times)", words=x["words"])
        else:
            with c.lock(p.dir):
                meta = c.read_meta(p)
                meta["taste_declined"] = ((meta.get("taste_declined") or []) + [x["words"] for x in pick])[-c.VETOES_KEEP:]
                c.write_meta(p, meta)
        c.log_event(p, "steer_compiled" if args.action == "compile" else f"taste_{args.action}",
                    data={"words": [x["words"] for x in pick]}, session=c.session_id())
        return cli.out(args, {"action": args.action, "proposals": pick},
                         done if args.action == "compile" else f"{'Adopted' if args.action == 'adopt' else 'Declined'}: "
                         + "; ".join(f"never {' '.join(x['words'])}" for x in pick) + ".")
    t = dict(taste(p, args.n, ledger), proposed=props, corrections=corr)
    lines = ["Kept (your requests, finished): " + ("; ".join(f"{k['id']} {k['title']}" for k in t["kept"]) or "none yet")]
    for head, rows in (("Dropped", [f"{d['id']} {d['title']}" + (f" — {d['why']}" if d["why"] else "") for d in t["dropped"]]),
                       ("Steered", t["steered"]), ("Corrected", t["corrected"])):
        lines += [f"{head}:"] + [f"  {r}" for r in rows] if rows else []
    if t["lenses"]:  # T-0607: which lenses' ideas the user keeps
        lines += ["Brainstorm lenses (ideas that became tasks):"] + [
            f"  {k}: {r['built']}/{r['ideas']} built, {r['dropped']} dropped, {r['open']} open"
            for k, r in sorted(t["lenses"].items(), key=lambda kv: -kv[1]["built"] / max(kv[1]["ideas"], 1))]
    if props:
        lines += ["Proposed vetoes (a no you keep steering with):"]
        lines += [f"  {i}. never {' '.join(x['words'])} — steered {x['count']} times, last: {x['said']}"
                  for i, x in enumerate(props, 1)]
        lines += ["Ask the user once (one AskUserQuestion, adopt first): yes → fm taste adopt (all) or fm taste adopt N; "
                  "no → fm taste decline N."]
    if corr:  # T-1042
        lines += ["Corrections met twice (a rule to propose):"]
        lines += [f"  {i}. {' '.join(x['words'])} — corrected {x['count']} times, last: {x['said']}\n     {x['suggest']}"
                  for i, x in enumerate(corr, len(props) + 1)]
        lines += ["Ask the user once (one AskUserQuestion, the rule first): yes → run its fm rule add line; "
                  "no → fm taste decline N."]
    cli.out(args, t, "\n".join(lines))


_CATEGORY = re.compile(r"(?m)^\s*[-*]\s*\*\*(.+?)\*\*.*?category:?\s*([\w][\w &/-]{0,30})\s*$")


_TITLE = re.compile(r"(?m)^\s*[-*]\s*\*\*(.+?)\*\*")
_QUESTION = re.compile(r"(?mi)^[\s*_>-]*Next question[*_]*:[*_]*\s*(.+?)\s*$")  # T-2076: one for the next brainstorm
NEAR = re.compile(r" — near T-\d+ \(done\).*$")
_WORD = re.compile(r"[a-z0-9]{3,}")


def _words(title):
    return {w[:5] for w in _WORD.findall(title.lower())}


IDEA_SECTIONS = re.compile(r"(?m)^## (?:Round \d+|Deepened: .*|Cross-bred)\s*$")


def seen_titles(text):
    """T-1056: the idea titles of an ideas.md, read only from its idea sections (Round, Deepened, Cross-bred): the
    per-lens, coverage and kill bullets below them are tallies, not ideas."""
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            on = bool(IDEA_SECTIONS.match(line))
        elif on and line.startswith("- "):
            out.append(NEAR.sub("", line[2:]).strip())
    return out


def _is_new(title, seen):
    """Not a near-repeat of an idea already on the list (most of its words shared)."""
    w = _words(title)
    return bool(w) and all(len(w & s) / len(w | s) < 0.6 for s in seen)


def deepen_pack(pack, category, titles):
    return (pack + f"\n\n## Category to build off: {category}\nIdeas in it so far:\n" + "\n".join(f"- {t}" for t in titles)
            + "\n\nYes-and: grow each into something bigger, combine them, and add the natural next ideas inside this "
              "category (don't repeat the list).")


def later_round_pack(pack, titles, n, thin=()):
    return (pack + "\n\n## Ideas so far (don't repeat these; go past them)\n" + "\n".join(f"- {t}" for t in titles)
            + (f"\n\nThin so far (fill these first): {', '.join(thin)}" if thin else "")
            + f"\n\nRound {n}: propose only NEW ideas: gaps nobody covered, second-order improvements on the ideas "
              f"above, combinations worth more together, and what a genuinely fully featured version would still lack.")


def coverage(results):
    """T-0605: ({category: ideas}, the axes with at most one idea)."""
    of, cats = {}, {}
    for r in results:  # distinct ideas: a later round repeating one doesn't count twice
        of.update(r.get("categories", {}))
    for cat in of.values():
        cats[cat] = cats.get(cat, 0) + 1
    thin = [a for a in AXES if sum(n for cat, n in cats.items() if a in cat or cat in a) <= 1]
    return cats, thin


FALSIFY = ("You kill ideas fast. For each numbered idea, give the quickest observation or experiment that would show "
           "it isn't worth building. Output only lines `N: <one line>`.")


def _child(args, p, system, prompt, detail, feature="ideas"):
    """T-2910: one brainstorm child's text: --model local goes through moonscale's routing (the first usable own or
    loopback generative profile, else Haiku; a sensitive project never leaves loopback) and books the answering model
    in args.booked; any other model runs `claude -p`."""
    if args.model != "local":
        return run_child(feature, system, prompt, args.model, args.timeout, project=p.slug, detail=detail)
    from sampo import moonscale  # lazy: moonscale imports this module at the top
    paid = sum(1 for b in list(args.booked) if moonscale._paid(b["model"]))  # T-2952: the Haiku children so far count to the cap
    text, model = moonscale._ask(p, system, prompt, feature=feature, paid=paid, timeout=args.timeout)
    args.booked.append({"detail": detail, "model": model})  # list.append is atomic: the round's threads share it
    return text


def _local_round(lenses, pack, system, args):
    """T-2910: [(text or None, error)] for a round's lenses, one thread per slot of the profile that answers first."""
    from concurrent.futures import ThreadPoolExecutor
    from sampo import moonscale
    p, sys_text = args.proj, system_text(system, pack)
    prof = moonscale._local(bool(c.read_meta(p).get("sensitive")))

    def one(lens):
        try:
            return _child(args, p, sys_text, lens_prompt(lens), lens), None
        except ValueError as e:  # a budget refusal, a sensitive project with no loopback model, an empty answer
            return None, str(e)
    # no local profile: Haiku answers, and a claude child has no slots, so one worker per lens
    with ThreadPoolExecutor(max(1, min(len(lenses), prof.get("slots", 1) if prof else len(lenses)))) as ex:
        return list(ex.map(one, lenses))


def _falsify(titles, args, p):
    prompt = "KILL IT FAST\n" + "\n".join(f"{i}: {t}" for i, t in enumerate(titles[:60], 1))
    text = _child(args, p, FALSIFY, prompt, "falsify")
    found = {int(m.group(1)): c.fit(c.plain(m.group(2)), 200) for m in re.finditer(r"(?m)^\s*(\d+):\s*(.+)$", text)}
    return {titles[i - 1]: f for i, f in found.items() if 0 < i <= min(len(titles), 60)}


def _crossbreed(titles, system, args, p):
    prompt = ("CROSS-BREED: combine the strongest of these ideas into 3-6 new ideas worth more together, in the "
              "required format.\n" + "\n".join(f"- {t}" for t in titles[:30]))  # not a list: titles for a model prompt
    text = _child(args, p, system, prompt, "crossbreed")
    return [c.plain(t).strip() for t in _TITLE.findall(c.redact(text))]


def _run_round(lenses, pack, system, args, out_dir, prefix, cli, cache=None):
    results = []
    try:
        warm = float(os.environ.get("FOREMAN_IDEAS_WARM") or WARM)
        with tempfile.TemporaryDirectory() as d:  # T-1265: one system text for the round, read by every lens child
            path = os.path.join(d, "system.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(system_text(system, pack))
            answers = _local_round(lenses, pack, system, args) if args.model == "local" else run_children(
                [(child_cmd(args.model, None, path), lens_prompt(lens), lens) for lens in lenses],
                args.timeout, "ideas", warm=warm, cache=cache)
    except OSError as e:
        raise cli.UsageError(str(e))
    for i, (lens, (out, why)) in enumerate(zip(lenses, answers), 1):
        ok, why = out is not None, why or ""
        partial = ok and bool(why)
        slug = re.sub(r"[^a-z0-9]+", "-", lens.lower()).strip("-") or "lens"
        path = os.path.join(out_dir, f"{prefix}{i:02d}-{slug}.md")
        text = c.redact(out.strip()) if ok else ""
        if ok and not _TITLE.search(text):  # an answer that isn't ideas (a refusal, a question) is a failed lens
            ok, partial, why = False, False, (f"{why}; " if why else "") + f"no ideas in the required format: {c.fit(c.plain(text), 150)}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Brainstorm — lens: {lens}\n\n" + (f"PARTIAL ({why})\n\n" if partial else "")
                    + (text if ok else f"FAILED: {why}") + "\n")
        results.append({"lens": lens, "file": path, "ok": ok, "partial": partial, "error": why,
                        "titles": [c.plain(t).strip() for t in _TITLE.findall(text)],
                        "categories": {c.plain(t).strip(): cat.strip().lower() for t, cat in _CATEGORY.findall(text)},
                        "questions": [q for q in (c.fit(c.plain(x).strip(), 200) for x in _QUESTION.findall(text)) if q][:2]})
    return results


def cmd_ideas(args):
    """One round of lenses, or a super brainstorm (--rounds N): each later round sees every idea so far and is asked
    only for new ones; it stops when a round adds fewer than --dry new ideas (T-0071)."""
    from sampo import cli
    c.refuse_if_paused()  # T-0591
    p = cli.resolve(args)
    pack = sys.stdin.read() if args.pack == "-" else open(args.pack, encoding="utf-8").read()
    args.proj, args.booked = p, []  # T-2910: --model local: the project, and the model that answered each child
    pack_sha = hashlib.sha256(pack.encode()).hexdigest()  # T-0959: the pack as given, for the manifest
    voice = user_voice(p)
    pack += "" if voice.strip() and voice.strip() in pack else voice  # T-2249: fm mission's pack carries it already
    missed, left = misses(p), questions(p)  # T-2076: what earlier brainstorms failed to predict, and asked to ask next
    if missed:
        pack += ("\n\n## What earlier brainstorms missed (asks the user had to make that no idea predicted; data)\n"
                 + "\n".join(f"- {i}: {t}" for i, t in missed))
    if left:
        pack += "\n\n## Questions earlier brainstorms left (answer them through your lens)\n" + "\n".join(f"- {q}" for q in left)
    lenses = list(dict.fromkeys(args.lens or DEFAULT_LENSES + ["blind spots"] * bool(missed)))
    if "oblique provocation" in lenses:  # T-0628: its provocations are the project's own lessons
        lessons = [ln.strip("- ").strip() for b in sorted(c.load_briefs(p), key=lambda b: b.meta.get("updated") or "")
                   for ln in b.section("Lessons").splitlines() if ln.strip()][-8:]
        if lessons:
            pack += "\n\n## Past lessons (provocations for the oblique lens)\n" + "\n".join(
                f"- {c.fit(c.plain(x), 200)}" for x in lessons)
    deepen = max(0, getattr(args, "deepen", 0) or 0)
    pace = budget.degrade(p) if deepen else None
    if pace:  # T-0449: optional rounds go first; the caps below still refuse what's over one
        print(f"sampo: optional work is dropped ({pace}): --deepen dropped (0 rounds); the lenses still run",
              file=sys.stderr)
        deepen = 0
    runs = len(lenses) * max(1, args.rounds) + deepen
    try:
        budget.check("ideas", budget.estimate("ideas", runs, 0.06), "fewer --lens, --rounds or --deepen")
    except budget.BudgetError as e:
        raise cli.UsageError(str(e))
    with open(PROMPT, encoding="utf-8") as f:
        system = f.read()
    out_dir = os.path.join(p.dir, "research", "brainstorm-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "status.json"), "w", encoding="utf-8") as f:  # T-0124: the dashboard's progress
        json.dump({"lenses": lenses, "rounds": max(1, args.rounds), "started": c.now()}, f)
    if len(pack.split()) > PACK_WORDS:
        print(f"sampo: warning: the pack is {len(pack.split())} words; every lens and round pays for it (aim for "
              f"≤ {PACK_WORDS})", file=sys.stderr)
    results, titles, seen, by_round, rounds, lens_yield = [], [], [], [], 0, dict.fromkeys(lenses, 0)
    if getattr(args, "seen_latest", None):  # T-2559: the newest earlier runs that left an ideas.md, this one excluded
        runs = sorted(glob.glob(os.path.join(p.dir, "research", "brainstorm-*", "ideas.md")), reverse=True)
        args.seen = (args.seen or []) + [x for x in runs if os.path.dirname(x) != out_dir][:max(0, args.seen_latest)]
    for path in args.seen or []:  # earlier brainstorms' ideas.md: don't repeat them, go past them (recursion)
        try:
            with open(path, encoding="utf-8") as f:
                old = seen_titles(f.read())
        except OSError as e:
            raise cli.UsageError(f"can't read --seen {path}: {e.strerror}")
        for t in old:
            if _is_new(t, seen):
                seen.append(_words(t))
                titles.append(t)
    known = len(titles)
    cache = {}  # T-1265: prompt-cache tokens read and written, every child of the run
    for n in range(1, max(1, args.rounds) + 1):
        rounds = n
        prefix = f"r{n}-" if args.rounds > 1 else ""
        got = _run_round(lenses, pack if not titles else later_round_pack(pack, titles, n, coverage(results)[1]),
                         system, args, out_dir, prefix, cli, cache)
        results += [dict(r, round=n) for r in got]
        new = []
        for r in got:
            for t in r["titles"]:
                if _is_new(t, seen):
                    seen.append(_words(t))
                    new.append(t)
                    lens_yield[r["lens"]] += 1
        titles += new
        by_round.append(new)
        if n > 1 and len(new) < args.dry:
            break
    deepened = {}
    if deepen:  # T-0099: build off the biggest categories, one yes-and child each
        cats = {}
        for r in results:
            for t, cat in r.get("categories", {}).items():
                cats.setdefault(cat, []).append(t)
        top = sorted(cats, key=lambda k: -len(cats[k]))[:deepen]
        got = []
        for cat in top:  # one child per category, each with its own pack
            got += [dict(r, round=rounds + 1, category=cat) for r in
                    _run_round([f"deepen: {cat}"], deepen_pack(pack, cat, cats[cat]), system, args, out_dir,
                               f"deep-{len(got) + 1}-", cli, cache)]
        results += got
        for r in got:
            new = [t for t in r["titles"] if _is_new(t, seen)]
            for t in new:
                seen.append(_words(t))
            titles += new
            deepened[r["category"]] = new
    kills, bred = {}, []
    try:
        kills = _falsify(titles[known:], args, p) if getattr(args, "falsify", False) and titles[known:] else {}
        bred = [t for t in (_crossbreed(titles[known:], system, args, p) if getattr(args, "crossbreed", False)
                            and titles[known:] else []) if _is_new(t, seen)]
    except ValueError as e:  # an optional pass that fails leaves the catalogue as it is
        print(f"sampo: {e}", file=sys.stderr)
    titles += bred
    cats, thin = coverage(results)
    near = recall.nearest_done(p, titles[known:])  # T-0208: what may already be built, before grounding
    item = lambda t: f"- {t}" + (f" — near {near[t][0]} (done): {c.fit(near[t][1], 60)}" if t in near else "") + "\n"
    with open(os.path.join(out_dir, "ideas.md"), "w", encoding="utf-8") as f:
        f.write("# Ideas by round (deduplicated titles; details in the lens files; \"near T-…\" names a finished task "
                "that shares most of an idea's words)\n"
                + f"Ideas: {len(titles) - known} distinct (the bullets under Round, Deepened and Cross-bred; the tallies "
                  f"below are counts, not ideas)\n"
                + "".join(f"\n## Round {i}\n" + "".join(map(item, ts)) for i, ts in enumerate(by_round, 1))
                + "".join(f"\n## Deepened: {cat}\n" + "".join(map(item, ts)) for cat, ts in deepened.items())
                + "".join(f"\n## Cross-bred\n" + "".join(map(item, bred)) for _ in [0] if bred)
                + "\n## New ideas per lens\n" + "".join(f"- {k}: {v}\n" for k, v in lens_yield.items())
                + "\n## Coverage (ideas per category)\n" + "".join(f"- {k} {v}\n" for k, v in
                                                                   sorted(cats.items(), key=lambda kv: -kv[1]))
                + f"Thin (0-1 ideas): {', '.join(thin) or 'none'}\n"
                + ("\n## Kill it fast (the quickest test that would show it isn't worth building)\n"
                   + "".join(f"- {t} — {k}\n" for t, k in kills.items()) if kills else ""))
    with open(os.path.join(out_dir, "ideas.json"), "w", encoding="utf-8") as f:  # T-0607: for fm taste's keep rates
        json.dump({"lenses": {r["lens"]: r["titles"] for r in results if r["ok"]}, "crossbred": bred,
                   "kills": kills, "created": c.now(),
                   "questions": list(dict.fromkeys(q for r in results if r["ok"] for q in r.get("questions", [])))}, f)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:  # T-0959: what made this run
        json.dump({"pack_sha256": pack_sha, "model": args.model, "lenses": lenses, "seen": args.seen or [],
                   "rounds": rounds, "created": c.now(), **({"cache": cache} if any(cache.values()) else {}),
                   **({"children": args.booked} if args.model == "local" else {})}, f)
    haiku = sum(b["model"] == "haiku" for b in args.booked)
    failed = [f"{r['lens']} (round {r['round']})" for r in results if not r["ok"]]
    with c.lock(p.dir):
        c.log_event(p, "ideas", data={"dir": out_dir, "lenses": lenses, "rounds": rounds, "ideas": len(titles) - known,
                                      "failed": failed}, session=cli.session())
    cli.out(args, {"dir": out_dir, "rounds": rounds, "ideas": len(titles) - known, "results": results, "yield": lens_yield, "booked": args.booked},
              f"Brainstorm ideas in {out_dir} ({len(titles) - known} new distinct over {rounds} round(s); index: ideas.md):\n"
              + "\n".join(f"  {('part' if r['partial'] else 'ok  ') if r['ok'] else 'FAIL'}{'r' + str(r['round']) + ' ' if args.rounds > 1 else ''}"
                          f"{r['lens']}: {r['file'] if r['ok'] else r['error']}" for r in results)
              + (f"\n  dry after round {rounds}: it added {len(by_round[-1])} new" if rounds < args.rounds else "")
              + (f"\n  no local model answered {haiku} of {len(args.booked)} children: Haiku did (--model local)" if haiku else "")
              + (f"\n  prompt cache: {cache['read']} tokens read, {cache['written']} written" if any(cache.values()) else ""))
    if failed:
        raise cli.UsageError(f"brainstorm lens(es) failed: {', '.join(failed)} (the others are saved)")


ORACLE = """You write the test oracle for one software change from its specification alone: you never see the code,
so your examples can't mirror an implementation. Describe observable behaviour only (inputs, outputs, files, exit
codes, messages), never internal APIs or names you'd have to guess.
Reply in exactly this shape:
## Examples
- GIVEN <state> WHEN <action> THEN <observable result>
(5-12 lines: the main path, edge cases, errors and the criteria's own checks; for each criterion, a negative twin:
a near-miss input or state that must NOT pass, so a check that accepts anything is caught)
## Ambiguities
- <question> — <the readings, and how the tests would differ>
(or a single line "- none")
## Misreadings
- <the worst plausible misreading of the request> — <one question or probe at intake that would catch it>
(1-3 lines, worst first)"""


def cmd_oracle(args):
    """T-0226: behaviour examples and ambiguities for a task from its request, interpretation and criteria only (a
    tool-less child in a scratch folder: no code, no repo), saved in the brief's Oracle section before tests are written."""
    from sampo import cli
    c.refuse_if_paused()  # T-0591
    p = cli.resolve(args)
    b = cli.need_brief(p, args.id)
    spec = "\n\n".join(f"## {name}\n{b.section(name).strip()}" for name in
                        ("Raw request", "Interpretation", "Acceptance criteria", "Non-goals") if b.section(name).strip())
    spec = f"Task: {b.title} ({b.type})\n\n{spec or b.title}\n"
    try:
        args.proj, args.booked = p, []
        text_out = _child(args, p, ORACLE, spec, b.id, feature="oracle")
    except ValueError as e:
        raise cli.UsageError(str(e))
    parts = {k: [] for k in ("Examples", "Ambiguities", "Misreadings")}
    head = None
    for line in text_out.splitlines():
        m = re.match(r"#+\s*(Examples|Ambiguities|Misreadings)\b", line.strip(), re.I)
        if m:
            head = m.group(1).capitalize()
        elif head and line.strip().startswith("- "):
            parts[head].append(c.fit(c.defang(c.redact(line.strip()[2:].strip())), 300))
    examples = parts["Examples"]
    ambiguities = [x for x in parts["Ambiguities"] if x.lower().strip(". ") != "none"]
    if not examples:
        raise cli.UsageError(f"no examples came back ({c.fit(c.plain(text_out), 160)})")
    text = ("Examples from the request alone, before the code was read (write tests from these):\n"
            + "".join(f"- {x}\n" for x in examples)
            + ("Ambiguities (decide each with fm decide, or ask, before the tests):\n" + "".join(f"- {x}\n" for x in ambiguities)
               if ambiguities else "Ambiguities: none found.\n")
            + ("Misreadings (the worst plausible ones; probe each at intake, T-0621):\n"
               + "".join(f"- {x}\n" for x in parts["Misreadings"]) if parts["Misreadings"] else ""))
    cli.mutate(p, b.id, lambda br: br.set_section("Oracle", text), "oracle",
                 {"examples": len(examples), "ambiguities": len(ambiguities)})
    cli.out(args, {"examples": examples, "ambiguities": ambiguities},
              f"{b.id}: oracle saved — {len(examples)} example(s), {len(ambiguities)} ambiguit"
              f"{'y' if len(ambiguities) == 1 else 'ies'}\n" + text)
