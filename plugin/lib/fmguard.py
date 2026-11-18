"""Ward, Sampo's guard: decides whether a tool call is dangerous enough to block (PROTECTED CORE).

Pure functions, no state writes. The PreToolUse hook calls check(); a Block is denied unless the
active brief's `allow:` lists its category. `state-direct` is never authorizable. Callers treat any
exception as a block (fail closed). This is a speed bump with good coverage, not a sandbox: shell
text can always be obfuscated; deny rules and git are the other layers.
"""
import collections
import functools
import json
import math
import os
import re
import stat
import sys
import shlex


class lazy:  # fmcore.lazy's twin: this module imports no other Foreman module
    """T-2020: a module-level pattern compiled on first use."""

    def __init__(self, pattern, flags=0):
        self._args = (pattern, flags)

    def __getattr__(self, name):
        if name.startswith("__") or name == "_args":
            raise AttributeError(name)
        attr = getattr(re.compile(*self._args), name)
        setattr(self, name, attr)
        return attr


CATEGORIES = ["self-authorize", "state-direct", "core", "remote", "plugin", "credentials", "system", "rm-outside",
              "git-destructive", "pipe-shell", "publish", "confirm", "confine", "brief"]
# confine (T-0234): a builder writes in its worktree only; brief (T-0308): no edits without a task — start one
NOT_AUTHORIZABLE = {"state-direct", "self-authorize", "confine", "brief"}
# T-2745: Sampo is Foreman's new name; the guard refuses under each sampo name what it refuses under the fm one.
# Every site that keys on `fm` reads these. The env twins are matched as (?:FOREMAN|SAMPO)_X, the module prefixes by
# startswith, the verb twins by _fm_subcommand.
KNOWS_SAMPO = True
FM_PROGRAMS = ("fm", "sampo")  # the program, and the script a python call runs: plugin/bin/<name>
FM_SCRIPT_ENDS = tuple("/" + p for p in FM_PROGRAMS)
FM_MODULE_PREFIXES = ("fm", "sampo")  # imports of fm* or sampo, sampo.* are Foreman's modules
FM_NAME_RE = "(?:fm|sampo)"
GUARD_FILES = ("fmguard.py", "sampo/ward.py")  # the guard under its old and new path (= _sampo_name("fmguard"))
VERB_TWINS = {"rune": "genesis", "louhi": "exam", "ward": "guard", "smith": "code", "moons": "genesis moonshot",
              "underworld": "genesis"}  # ponytail: no rule keys on these verbs yet; a later one reads the old name
# fm subcommands that undo or grant what only the user decides: sub -> (the word that makes it so, why, what they type).
# One table, so the next one isn't forgotten.
USER_ONLY_FM = {
    "posture": ("lite", "sampo posture lite turns Sampo's hooks off but the guard's: the user decides that",
                "sampo posture lite"),  # T-0963
    "pause": ("off", "sampo pause off is the user's to run: they paused everything Sampo runs unattended, so only they "
                     "lift it", "sampo pause off"),  # T-0591
    "approve-reads": ("on", "sampo approve-reads on lets Sampo answer Claude Code's permission dialogs for reads: the "
                            "user turns it on", "sampo approve-reads on"),  # T-0974
    "rule": ("rm", "sampo rule rm is the user's to run: their rule stops what they said to stop, so only they lift it",
             "sampo rule rm N"),  # T-0971; `add rm` is a rare miss
    "vetoes": ("rm", "sampo vetoes rm is the user's to run: their veto keeps an idea out of the plan, so only they lift it",
               "sampo vetoes rm N"),  # T-1413
    "taste": ("--block", "sampo taste compile N --block makes a rule that stops a command outright, and only the user lifts it "
                         "(sampo rule rm): they decide what blocks", "sampo taste compile N --block"),  # T-1063
    "rollback": ("--yes", "sampo rollback --yes is the user's to run: it puts back an older state, with consent they "
                          "may have revoked since", "sampo rollback --yes"),  # T-1085
    "decide": ("--answer", "sampo decide ANSWER --answer N records the user's answer to an ask, and answers become "
                           "precedent for proxy answers: only the user records them", "sampo decide ANSWER --answer N"),  # T-1064
    "risk": (("set", "off"), "sampo risk set and sampo risk off move or lift the daily risk budget that slows Claude's "
                             "autonomy: the brake is the user's to move (/fm-risk off, typed)",
             "sampo risk set --day N --because \"<why>\""),  # T-1080, T-3752
    "home": ("import", "sampo home import is the user's to run: it writes a whole Sampo state, settings included, from "
                       "a file", "sampo home import FILE"),  # T-1665
    "prove": ("--yes-run", "sampo prove --yes-run is the user's to run: it says yes to running this tree's tests, as "
                           "them, with the network, now and from then on", "sampo prove PATH --yes-run"),  # T-2122, T-2202
    "mandate": (("set", "off"), "sampo mandate set and sampo mandate off are the user's to run: they end, overwrite or renew "
                                "the standing mandate they gave", "sampo mandate set \"<text>\""),  # T-2252
    "triggers": ("set", "sampo triggers set is the user's to run: a queue or lane route starts work no one asked for, and "
                        "a mute or budget silences a signal", "sampo triggers set NAME --route inbox"),  # T-2355
}
FM_GATED = {"plugins", "night", "agents", "serve", "cpu"}  # T-3600 (serve, cpu: T-4785): other verbs whose check in
# check_bash keys on a later word; a new such check adds its verb here, so a word the shell fills in there is refused
# too (_fm_unread)
FM_TRAILING = ("quiet", "at")  # T-4785 round 12: verbs whose trailing words are a command sampo runs (_fm_runs)
FM_STORING = ("task", "intake", "check", "smoke", "notify", "record", "whyred") + FM_TRAILING  # the verbs _fm_stored and
# _fm_runs read a command from: a new one adds its verb here. With USER_ONLY_FM and FM_GATED they make _NET_VERBS, and a
# test scans the sources for a verb literal missing from it


def _pause_is_agents(ctx):
    """T-4190: sampo pause off is the agent's to run only over a pause a session's own tool call set. The flag file says
    so (line 2, JSON, by == "agent"): read here by hand, no import of Sampo. It fails closed: some PANIC file must exist,
    and every one that does (each state folder the guard knows) must be a plain file that reads exactly by=agent; a
    missing one, an old one with no record, a broken or forged one, a folder in its place, is the user's."""
    seen = False
    for d in ctx.state_dirs:
        path = os.path.join(d, "PANIC")
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        seen = True
        try:
            with open(path, "rb") as f:
                f.readline(512)
                rec = f.readline(4096)
            by = json.loads(rec).get("by")
        except (OSError, ValueError, AttributeError):
            return False
        if by != "agent":
            return False
    return seen


def _user_only_hit(word, arg):
    """A word matches itself; a --flag also matches argparse's abbreviations (--y, --ye) and --flag=value.
    A tuple of words matches any one of them."""
    if isinstance(word, tuple):
        return any(_user_only_hit(w, arg) for w in word)
    a = arg.split("=")[0] if word.startswith("--") else arg
    return a == word or (word.startswith("--") and len(a) >= 3 and a.startswith("--") and word.startswith(a))


USER_ONLY = {"core", "remote", "plugin", "confirm"}  # granted only by the user's reply to `fm ask`, never by `fm task set --allow`
# (confirm, T-0289: a request a child found in an old transcript is worked only after the user says it's theirs)
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_CORE_DIRS = ("lib", "bin", "hooks", "evals", "integrations", "native")  # plugin/…: Foreman's code (integrations:
# T-0340; native: T-3571, sampo-fast's source and the binary hooks/fast runs)
# T-1328: Foreman's own config that becomes a command (code.toml), runs Python (tui.toml, tui/) or picks where code is
# sent (models.json): an agent that writes one runs what it likes on the next fm code
_CORE_CONFIG = ("code.toml", "tui.toml", "models.json")
_CORE_CONFIG_DIRS = ("tui", "tui.projects")  # T-1611: extension panels, and each project's settings (they bind keys)
DEFAULT_BRANCHES = {"main", "master", "trunk"}


# Plain classes, no imports from the rest of Foreman: the guard must load (and fall back to its committed copy) even
# when the rest of the library is broken, and dataclasses would cost every hook ~9 ms of import.
class Ctx:
    def __init__(self, cwd, project_root, home, foreman_home, scratch=(), allow=(), task_id=None, state_dir=None,
                 state_fallbacks=(), standing=(), trusted=False, confine=None, unbriefed=None):
        self.cwd, self.project_root, self.home, self.foreman_home = cwd, project_root, home, foreman_home
        # T-1249: each scratch folder as given and as it really is (macOS's /tmp is a link to /private/tmp, and a
        # path is judged where it really points too)
        self.scratch = list(dict.fromkeys([*scratch, *(os.path.realpath(s) for s in scratch if s)]))
        self.allow, self.task_id = set(allow), task_id
        self.state_dir = state_dir  # when Foreman state lives outside foreman_home (read-only home fallback)
        # where a fallback could live: state even before it's used, as named and in its folder's real path (T-1284:
        # macOS's temp folder is a link). Never the leaf's link: another user can plant /tmp/foreman-state-<uid> -> /
        self.state_fallbacks = list(dict.fromkeys([*state_fallbacks, *(
            os.path.join(os.path.realpath(os.path.dirname(s)), os.path.basename(s)) for s in state_fallbacks if s)]))
        self.standing = set(standing)  # T-0119: the project's standing yeses (core only)
        self.trusted = bool(trusted)  # T-0120: /fm-trust on: the guard file and Claude Code settings too
        # T-0234: (a builder's worktree, its project's main checkout) — every write outside the worktree is refused but
        # scratch, and the main checkout is never scratch (even under /tmp)
        self.confine = confine  # T-1902: and each as it really is (macOS's temp folder is a link, as for scratch)
        self.confine_real = tuple(x and os.path.realpath(x) for x in confine) if confine else None
        self.unbriefed = unbriefed  # T-0308: the project's root while no task is active — writes inside wait for one
        self.lane = None  # T-3534: (lane folder, what to do there), set by the hook when a brief refusal's path is in one
        # T-4340: lane folders (worktree tops) whose one task is active there, set by the hook: the session itself has
        # no task, but a file strictly inside such a lane is that task's (the lane's root only for a merge in it)
        self.briefed_lanes = ()

    @property
    def state_dirs(self):
        """T-1318: every place state lives, judged the same way: Sampo's own state folder and an explicit one too
        (~/.claude linked into a dotfiles repo, FOREMAN_STATE under macOS's linked /tmp), each as named and with its
        folder resolved (the leaf never followed), then the fallbacks."""
        named = [d for d in (os.path.join(self.foreman_home, "state") if self.foreman_home else None, self.state_dir) if d]
        return list(dict.fromkeys([*named, *(os.path.join(os.path.realpath(os.path.dirname(d)), os.path.basename(d))
                                             for d in named), *self.state_fallbacks]))


class Block:
    def __init__(self, category, detail):
        self.category, self.detail = category, detail
        self.rule = rule_id(category, str(detail))  # T-0672: which check fired, for fire counts
        self.fallback = False  # T-3684: the words came from the quote-blind split (shlex couldn't read the line)

    def __repr__(self):
        return f"Block({self.category!r}, {self.detail!r})"


_FILLER = {"a", "an", "the", "it", "its", "is", "of", "to", "this", "s", "t"}  # T-1897: words a rule name reads past


def rule_id(category, detail):
    """T-0672: a stable name for the check behind a refusal: its category and the first words of its wording, with
    paths, quoted text, numbers and filler words taken out (rm -rf ~ and rm -rf /x are one rule)."""
    # T-1897: a quote opens and closes outside a word, so command's and hasn't are not one quoted span
    words = re.sub(r"\S*[/~$\\]\S*|\S*\d\S*|`[^`]*`|(?<!\w)'[^']*'(?!\w)|\"[^\"]*\"", " ", detail.lower())
    words = re.sub(r"\bsampo\b", "fm", words)  # T-2847: a respelled message keeps the id its counts are filed under
    ext = os.path.splitext(detail.split()[0])[1].lstrip(".").lower() if "/" in detail else ""  # T-0894: kinds apart
    path = ("path-" + ext if ext.isalnum() else "path") if "/" in detail else "other"
    return f"{category}:{'-'.join([w for w in re.findall(r'[a-z]+', words) if w not in _FILLER][:4]) or path}"  # not a list: a path or word fragment, not a list


_CLIMB = lazy(r"(?:^|[^\w.])\.\.(?:[^\w.]|$)")


def findings(tool_name, tool_input, ctx):
    """Every (category, detail) a tool call touches, allowed or not."""
    lanes = getattr(ctx, "briefed_lanes", ())
    if lanes and _CLIMB.search(str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
                               if tool_name in FILE_TOOLS else str(tool_input.get("command") or "")):
        # T-4340 review: the guard reads a/link/../x as a/x, the kernel goes through the link first: a lane's freedom
        # never reaches a call with a .. in it (the lexical path of a/link/../x is inside the lane, its real one isn't)
        ctx.briefed_lanes = ()
        try:
            return findings(tool_name, tool_input, ctx)
        finally:
            ctx.briefed_lanes = lanes
    if tool_name in FILE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        path, real = _walk(_expand(raw, ctx), ctx.cwd)
        found = _judge(classify_write, path, real, ctx)
        if _CARGO_CONFIG.search(path):  # T-3607 review
            text = [tool_input.get(k) for k in ("content", "new_string", "new_source")] + [
                e.get("new_string") for e in tool_input.get("edits") or [] if isinstance(e, dict)]
            found += _cargo_config_hit(path, "\n".join(str(t) for t in text if t), [ctx.cwd], ctx)
        return found
    if tool_name == "Bash":
        _DISCARDS[:], _DISCARDS_OF[0] = [], tool_input["command"]  # T-1482: filled by this check only
        _COMMITS.clear()
        _FELL_BACK.clear()
        return check_bash(tool_input["command"], ctx)
    if tool_name == "Read":  # T-1224
        path, real = _walk(_expand(str(tool_input.get("file_path") or ""), ctx), ctx.cwd)
        return _judge(classify_read, path, real, ctx)
    return []


def check(tool_name, tool_input, ctx, found=None):
    found = findings(tool_name, tool_input, ctx) if found is None else found
    for cat in CATEGORIES:
        for got, detail in found:
            if got == cat and (got in NOT_AUTHORIZABLE or got not in ctx.allow or unliftable(got, detail)):
                if got == "core" and ("core" in ctx.standing and _standing_covers(detail, ctx)
                                      or ctx.trusted and _trust_covers(detail, ctx)):
                    continue
                if got == "brief" and tool_name == "Bash" and _focuses(tool_input.get("command") or ""):
                    detail += _FOCUS_LATE  # T-0404: the line's own fm focus hasn't run when the guard reads it
                blk = Block(got, detail)
                blk.fallback = got == "system" and tool_name == "Bash" and bool(_FELL_BACK)
                return blk
    plug =[d for got, d in found if got == "plugin"]
    # T-0201: refreshing listings before one `claude plugin update` is that update (unpinned; it refreshes them itself)
    if plug.count(_UPDATE) == 1 and all(d in (_UPDATE, _MARKET_UPDATE) for d in plug):
        plug = [_UPDATE]
    if len(plug) > 1:  # a plugin yes is used up by one change (fmhooks)
        return Block("plugin", "more than one plugin change in one command; one yes covers one change: run each as "
                               "its own command, after its own sampo ask")
    return None


def _is_allow(arg):
    """`--allow`, `--allow=…` and any abbreviation argparse would have accepted (fm now rejects those too)."""
    flag = arg.partition("=")[0]
    return len(flag) > 3 and "--allow".startswith(flag)


_ASK = "sampo ask {id} {cat} --why \"<what and why>\", then ask the user one yes/no question; their yes grants it"


_CD_FAILED = r" \(the cd to (\S+) may have failed\)$"  # T-3713 (detail suffix; check_bash adds it)
_FOCUS_LATE = " (this command's own sampo focus hasn't run yet: the guard reads the whole command first)"


def _focuses(cmd):
    """T-0404: whether a command focuses a task itself (fm focus, fm task new … --focus)."""
    try:
        return any(a[:1] == ["focus"] or (a[:2] == ["task", "new"] and "--focus" in a) for a in fm_calls(cmd))
    except Exception:
        return False


def _hint(detail, cat=""):
    """T-0185: when the guard couldn't pin the target down, the rewrite that lets it (a false block cost a guess)."""
    research = (" If it is a download or notes for the project's research, write it to the scratchpad and file it with "
                "sampo research add NAME --file PATH (it replaces an existing NAME; add --append to keep it).") if cat == "state-direct" else ""  # T-0814
    if "(not known before it runs" in detail:
        return (" If the target is elsewhere, name it literally or set its variable once before any loop, pipe or "
                "branch (D=/path; for …), and chain a cd with &&, so the guard can read where it writes." + research)
    if "unresolvable target" in detail:
        return (" If it is elsewhere, name the path literally, or set the variable once before any loop or pipe."
                + research)
    if re.fullmatch(r"downloaded content piped into python[0-9.]*", detail):  # T-0395
        return (" A python3 -c that only reads the download as data (json.load(sys.stdin)) passes when every command "
                "before it, or after it in its pipeline, is a plain data tool that writes no file (curl -s, jq, head, "
                "tail, sleep) or sets a new name (T=…), and no & runs anything beside it: run a program that writes "
                "files, or a redirect, as its own command or after python's pipeline (; git log).")
    if "is written earlier in this command and run here" in detail:  # T-1289
        return " Write the script in one command, run it in the next command (Edit/Write tool or cat > f <<'EOF')."
    if detail == "script nests too deep to read":  # T-1298
        return " Run the innermost script directly, or fold the chain into fewer scripts, so the guard reads each one."
    if detail.endswith(_DEEP):  # T-1931
        return (" Flatten the script's nested bash -c / eval layers (run the inner command itself), so the guard "
                "reads all of it.")
    if detail.endswith("the script was too long to parse in time)"):
        return " Run a shorter script (split it, or run the part you need directly): a shorter one is read in full."
    if detail.startswith("sed -f/--file"):  # T-1932
        return " Pass the sed commands with -e (the guard reads them), or make the change with the Edit tool."
    if "cargo --config FILE" in detail or "rustc @FILE" in detail:  # T-3607 review
        return " Pass the settings or arguments inline (--config KEY=VALUE, rustc's flags themselves): the guard reads those."
    if detail.startswith("vim ") and "can't read" in detail:
        return " Pass the commands with -c (the guard reads them), or make the change with the Edit tool."
    if detail.startswith("IFS") and "can't read" in detail:
        return " Leave IFS at its default on a line that splits words, or set it inside a script file the guard reads."
    cdf = re.search(_CD_FAILED, detail) if cat == "brief" else None
    if cdf:  # T-3713: the target is in the project only if that cd failed: say which, and the chain that fixes it
        return (f" The cd to {cdf[1]} may fail, so the write may land here: chain it with && (cd {cdf[1]} && …) "
                "so the write runs only there.")
    if cat == "brief" and os.path.basename(re.sub(_CD_FAILED, "", detail)).lower() == "changelog.md":  # T-1452
        return (" A changelog line belongs to the task it describes: add it before sampo task finish. Once that task is "
                "done, add the line in the next task and say why there (sampo task log ID \"scope: …\").")  # T-1767
    if detail.endswith(_FOCUS_LATE):
        return " Run the sampo focus (or sampo task new … --focus) as its own command, then this one."
    hop = re.search(r"; git merge, the cd to (.+) may not hold\)$", detail)
    if hop:  # T-3553: meant for a worktree, not the main checkout: sampo lane merge is no way there
        return (f" Run cd {shlex.quote(hop[1])} && git merge … as its own command, the folder named literally: on this line the guard "
                "can't tell the cd holds (a cd that may fail or be skipped, or a later loop or substitution), so it reads "
                "the merge as maybe running here too. A build-branch merge in a lane's own folder passes.")
    if detail.endswith("; git merge)"):
        return (" On Sampo's own repo, land a reviewed builder branch with sampo lane merge ID: it merges --no-ff and "
                "checks each file it changes as that task's write.")
    if cat == "core" and detail.endswith("(a tree write over it)"):  # T-1897: 21 in a week, none with a hint
        return (" Run git checkout, reset, cherry-pick or rebase in a scratch worktree (git worktree add <scratch>/x, "
                "then cd <scratch>/x && git …). Sampo's own tree changes through a task: sampo edit or the Edit tool per "
                "file (with a standing core yes: sampo ask ID core --standing), sampo task finish to commit.")
    if re.match(r"interpreter code driving (?:Sampo|Foreman)'s modules", detail):  # review of T-1897: 17 in a week, no hint
        # T-4921: prose for a task or a capture is no source file: a draft file, passed with --file, not sampo edit
        return (" A draft that only names them goes through the Write tool, then sampo edit < draft as the next command; "
                "prose for a task goes in a file the Write tool makes, passed as sampo task set ID --section S --file PATH.")
    if detail.endswith("(written from interpreter code)"):
        return (" If the script only writes other files, keep it to builtin open() on literal paths with no import but "
                "re, json, textwrap or sys, so only those count, or use the Edit tool.") + research
    return research


UNANALYSED = frozenset({  # T-1929: refusals for text the guard never read to its end; each names its way through
    "command nesting too deep to analyse: split the command",
    "nested commands in too many folders to analyse: split the command",
    "the line may be in too many folders to analyse: split the command",
    "eval of computed text it can't analyse: run the text itself",
})


def unanalysed(detail):
    """T-1929: no grant lifts these: the rest of the line was never read, so a grant would let it run unchecked."""
    return isinstance(detail, str) and detail in UNANALYSED


_DEEP = "nested too deep to read in full)"  # T-1931: a script's own nesting past the cap, scanned for literals only


def _unread(detail):
    """A system refusal for a program the guard couldn't read (not for what a program does). T-1934: by the guard's
    own wording, so a path holding "can't read" doesn't count"""
    return "guard can't read" in detail or detail == "script nests too deep to read" or detail.endswith(
        ("the script was too long to parse in time)", _DEEP)) or "KB the guard reads: run its" in detail


def unliftable(cat, detail):
    """T-1932: no grant lifts a refusal for text the guard never read (to its end): it would run unchecked."""
    return unanalysed(detail) or cat == "system" and isinstance(detail, str) and _unread(detail)


def message(block, ctx):
    detail = block.detail if isinstance(block.detail, str) else ""
    where = ""  # T-1125: a shell left inside state/ by an earlier cd turns a relative path into a state write
    if detail.endswith("(written from interpreter code)") and _under(ctx.cwd or "", os.path.join(ctx.foreman_home, "state")):
        where = f" The shell is in {ctx.cwd}, so a relative path lands there: use an absolute path or cd out first."
    msg = _message(block, ctx) + where + _hint(detail, block.category)
    if getattr(block, "fallback", False):  # T-3684: a parse failure, not a command: the fix comes before any grant
        msg = ("Ward: the guard could not parse this line's quoting (nested \" or $( ) inside a quoted program), so it "
               "read words inside the string as commands. Put the program in a heredoc (node <<'EOF' … EOF) or a "
               "file, which it can read; no grant is needed for that. (Refusal as read: " + msg.removeprefix("Ward: ") + ")")
    return msg


def _message(block, ctx):
    cat, detail = block.category, block.detail
    tid = ctx.task_id or "<ID>"
    if cat == "self-authorize":
        return (f"Ward: blocked self-authorize: {detail}. Protected core (Sampo code, rules, evals, "
                f"BUILD_PROMPT.md, settings) and remote sessions need the user's approval, which an agent can't grant: "
                + _ASK.format(id=tid, cat="<" + "|".join(sorted(USER_ONLY)) + ">") + ".")
    if cat == "confine" and str(detail).startswith("sampo check"):  # T-4785
        return (f"Ward: blocked confine: {detail}. A builder runs the gates (sampo check, check list, check office) but "
                f"never changes them: report the gate change you need to the main thread, which sets the project's gates.")
    if cat == "confine":
        return (f"Ward: blocked confine: {detail} is outside this builder's worktree ({ctx.confine[0]}). A builder "
                f"writes only there (and in scratch); report what needs changing elsewhere to the main thread.")
    if cat == "brief" and getattr(ctx, "lane", None):  # T-3534: the lane's task, never a new one in the main checkout
        return f"Sampo: no active task in lane {ctx.lane[0]}, so {detail} can't be edited yet.{ctx.lane[1]}"
    if cat == "brief":
        return (f"Sampo: no active task in this project, so {detail} can't be edited yet. One command starts a small "
                f"task: sampo task new \"<title>\" --type FIX --tier S --ac \"<done when>\" --step \"<step>\" --focus "
                f"(bigger work: /sampo:intake; sampo next says what's next).")
    if cat == "state-direct":
        diff = re.search(r"/audits/(T-\d+)\.diff$", str(detail))  # T-0413
        return (f"Ward: blocked state-direct: {detail} is Sampo state. Change it through fm "
                f"(sampo task …, sampo capture, sampo checkpoint{f'; sampo audit prep {diff[1]} rewrites it' if diff else ''}); "
                f"direct writes are never authorized.")
    if unliftable(cat, detail):  # T-1298, T-1929, T-1932: the way through is text the guard reads (the hint)
        return f"Ward: blocked {cat}: {detail}; no grant lifts this."
    if ctx.task_id:
        how = f"sampo task set {ctx.task_id} --allow {cat} (records it in the brief), then retry"
    else:
        how = (f"create or focus a task first (sampo task new \"…\" --type T --tier S, or sampo focus ID), "
               f"then sampo task set <ID> --allow {cat}, then retry")
    trusted, standing = getattr(ctx, "trusted", False), "core" in (getattr(ctx, "standing", None) or ())
    # (T-3608 round 7: not the installer's finding: a build isn't a change plain file edits make, the user grants it)
    if cat == "core" and (trusted or standing) and _plain_path(detail) is None and detail != _INSTALLER_CORE:
        # T-2859: the yes covers this change as plain file edits; only the form can't be read ('stop asking me')
        yes = "Trust (/fm-trust on)" if trusted else "The standing core yes"
        return (f"Ward: blocked core: {detail}. {yes} covers this change made as plain file edits, not in "
                f"this form: use the Edit or Write tool on each file (or a draft, then sampo edit < draft). No "
                f"question for the user.")
    if cat == "core" and detail == _INSTALLER_CORE:  # T-4827: the trust and standing branches above skip it (a build isn't a file edit)
        return _INSTALLER_SAYS.format(id=tid)
    if cat in USER_ONLY:  # rules/foreman.md, README and MASTER name these categories too
        what = f"{detail} is protected core" if cat == "core" else detail
        return f"Ward: blocked {cat}: {what}. To authorize: " + _ASK.format(id=tid, cat=cat) + "."
    return f"Ward: blocked {cat}: {detail}. To authorize: {how}."


def project_root_for(cwd, home):
    d = os.path.realpath(cwd)
    probe = d
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    h = os.path.realpath(home)
    if d == "/" or d == h or h.startswith(d.rstrip("/") + "/"):
        return None
    return d


# ---------------------------------------------------------------- paths

def _expand(tok, ctx):
    t = re.sub(r"^~(?=/|$)", lambda _: ctx.home, tok)
    if re.match(r"~[\w.-]+(/|$)", t):  # ~user (T-0175 review)
        t = os.path.expanduser(t)
    return t.replace("${HOME}", ctx.home).replace("$HOME", ctx.home)


def _unresolvable(tok):
    return "$" in tok or "`" in tok or bool(re.match(r"~[+-]", tok))  # ~+ and ~- are $PWD and $OLDPWD


def _unliteral(tok):
    """T-3600: a word the shell may turn into other words at run time: an expansion, or, outside quotes (a word holding
    a space was quoted), a brace list. ponytail: a glob (ser?e) is read as written: it becomes a verb only through a
    file of that name in the folder, and grep patterns quoted inside $( ) read as globs here (fm [a-z])."""
    return _unresolvable(tok) or not re.search(r"\s", tok) and bool(re.search(r"\{[^{}]*(?:,|\.\.)[^{}]*\}", tok))


def _resolve(tok, base):
    return os.path.normpath(tok if os.path.isabs(tok) else os.path.join(base, tok))


def _run_file_text(path, ctx, depth):
    """T-4139: `sampo task evidence --run-file PATH` runs the script as `bash PATH` runs it, so the guard judges it the
    same way (_script_verdict: the time budget, the chunks, the cache and the script rules, a $VAR target included).
    A path a variable or a glob names is a finding (the CLI runs no path it can't read as written). A missing file
    has no findings here: the CLI refuses to run it, and judges the bytes it does run itself."""
    if not path or re.search(r"[$`*?\[]", path):
        return [("system", f"--run-file {path or '(no path)'}: a path a variable or a glob names can't be checked: name "
                           "the script's own path")]
    return _script_verdict(os.path.realpath(_resolve(_expand(path, ctx), ctx.cwd)), ctx.cwd, "sh", ctx, depth)


def _walk(tok, base):
    """T-4667 (after T-4666): (path, real). `path` is _resolve's (normpath collapses a '..' before the kernel would); the
    kernel follows a link before it climbs, so a/link/../x is not a/x: `real` is where the raw token really goes (each
    prefix resolved, then climbed), "" when there is no '..' in it or the climb leaves the path where _variants already
    looks (its realpath). A plain pair, not an attribute of the string: a string operation drops an attribute, and
    only a climbing token pays for the walk."""
    joined = tok if os.path.isabs(tok) else os.path.join(base, tok)
    path = os.path.normpath(joined)
    if ".." not in joined.split("/"):
        return path, ""
    try:
        real = os.path.realpath(joined)
        return path, "" if real in (path, os.path.realpath(path)) else real
    except (OSError, ValueError):
        return path, ""


def _judge(classify, path, real, ctx, note=""):
    """T-4667: [(category, detail)] of `path` and, when a '..' came after a link, of where it really goes, whatever the
    category: the stricter reading wins (every category either reading reaches counts). A category only the real path
    reaches names the real file, and its detail carries a ' (' so that no standing or trusted yes covers it: a path
    that climbs out of a link is asked about each time."""
    found = [(cat, f"{path}{note}") for cat in classify(path, ctx)]
    if real:
        seen = {cat for cat, _ in found}
        found += [(cat, f"{real}{note} (a '..' after a link in {path})") for cat in classify(real, ctx) if cat not in seen]
    return found


def _under(path, root):
    root = root.rstrip("/")
    return not root or path == root or path.startswith(root + "/")


def _strictly_under(path, root):
    return path != root.rstrip("/") and _under(path, root)


def _variants(path):
    out = {path}
    try:
        out.add(os.path.realpath(path))
    except OSError:
        pass
    return out


_SAFE_DEV = lazy(r"^/dev/(null|zero|stdout|stderr|stdin|tty|random|urandom|fd/.*|shm/.*)$")
_DOC_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".java", ".kt", ".rb",
            ".php", ".cs", ".swift", ".scala", ".sh", ".md", ".rst", ".adoc", ".html", ".css", ".nix", ".lua", ".zig", ".ml"}
_CRED_DIRS = [".ssh", ".aws", ".config/gcloud", ".kube", ".gnupg", ".config/gh",
              ".config/sampo"]  # T-3188: secrets.env, selfgate.key and any later key
_CRED_FILES = [".netrc", ".npmrc", ".pypirc", ".git-credentials", ".claude/.credentials.json", ".docker/config.json"]


def _is_credential(path, ctx):
    """Whether a file's path or name says it holds a secret. T-4666: a file that is one inode with another name
    (a hardlink: `ln .env .env.example`) is judged by the strict reading, which no release (scratch, template,
    design) applies to, as its twin's name cannot be told from here."""
    if _credential_named(path, ctx):
        return True
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1 and _credential_named(path, ctx, strict=True)


def _home_credential(path, ctx):
    """T-4667: whether a path is one of the home's credential folders or files, in any letter case (~/.SSH/id_rsa is the
    same file on a case-insensitive disk, and a real name only differs by case there)."""
    low = path.lower()
    return (any(_under(low, os.path.join(ctx.home, d).lower()) for d in _CRED_DIRS)
            or low in {os.path.join(ctx.home, f).lower() for f in _CRED_FILES})


def _credential_named(path, ctx, strict=False):
    if _home_credential(path, ctx):
        return True
    base = os.path.basename(path).lower()  # T-4666: .ENV and KEY.PEM are the same files on a case-insensitive disk
    if os.path.isdir(path):
        return False  # T-4143: a folder named tokens/.env holds no secret itself (a file in it is judged by its own name)
    # T-4143: a scratch fixture (redchk/host/.env under the session scratchpad, /tmp, a test's temp folder) holds what
    # the session wrote there: no secret of the user's. A scratch root that contains the home or the project (a project
    # checked out under /tmp) never exempts, for any name (T-4666: T-0169's later release let secrets.yaml through):
    # only a folder strictly apart from both counts
    if not strict and any(_under(path, s) and not _under(ctx.home, s)
                          and not (ctx.project_root and _under(ctx.project_root, s))
                          for s in getattr(ctx, "scratch", ()) or ()):
        return False
    # T-4666: .default and .defaults hold live values; .dist and .tmpl are templates by convention
    if re.fullmatch(r"\.env(\..+)?", base) and (strict or not re.search(r"\.(example|sample|template|dist)$", base)):
        return True
    if re.search(r"\.(pem|key|p12|pfx|keystore|jks)$", base) or re.match(r"id_(rsa|dsa|ecdsa|ed25519)(?!.*\.pub$)", base):
        return True
    stem, ext = os.path.splitext(base)
    if not strict and (ext in _DOC_EXT or ext in (".example", ".sample", ".template", ".dist", ".tmpl")):
        return False  # T-0386: secrets.yaml.example is the placeholder copy, as .env.example is
    if re.search(r"(^|[._-])(secrets?|credentials?)([._-]|$)", stem):
        return True
    # a design system's tokens (colours, spacing, durations) aren't auth tokens (JARVIS: design/tokens.json was refused);
    # T-3908: nor is a UI source file named for them (gallery/Tokens.svelte): markup and styles, never a token store
    if not strict and ext in (".svelte", ".vue", ".jsx", ".tsx", ".astro", ".css", ".scss", ".sass", ".less", ".html"):
        return False
    if not re.search(r"(^|[._-])tokens?([._-]|$)", stem):
        return False
    # T-4666: only the stems tokens, design-tokens and *.tokens, and only for a design folder or a web asset folder
    # BELOW the project root (a ui/auth/api-tokens.json, or a /ui/ above the project, is no design file), never under
    # a dot folder. T-3810: and a web asset folder's inside the project (plugin/web/live/board-tokens.json): what a page
    # serves is no secret, so ~/.config/app/public/tokens.json stays one
    root = getattr(ctx, "project_root", None)
    rel = os.path.relpath(path, root).lower() if root and _under(path, root) else ""
    if strict or not rel or re.search(r"(^|/)\.", rel):
        return True
    dirs = "/" + os.path.dirname(rel) + "/"
    named = stem in ("tokens", "design-tokens", "design_tokens", "design.tokens") or stem.endswith(".tokens")
    if named and (re.search(r"/(design|themes?|styles?|ui)/", dirs) or stem.startswith("design")):
        return False
    return not (stem != "token" and re.search(r"/(web|www|static|assets|public|css|s[ac]ss)/", dirs))


_CRED_PUBLIC = {".ssh": {"known_hosts", "known_hosts.old", "authorized_keys", "authorized_keys2", "config"},
                ".aws": {"config"}}  # profiles and regions


# T-4736 review: what one process's /proc folder holds of another's secrets: its environment (every key it was started
# with), its memory and its open files. cwd, cmdline, status, stat and exe stay readable. A pid that is `self` is this
# process's own, but its environ is still what the guard keeps from a command's output (_prints_environment).
_PROC_LEAF = r"(?:environ|mem|maps|smaps|smaps_rollup|fd/[^/]*|fdinfo/[^/]*)"
_PROC_SECRET = lazy(rf"/proc/(?P<pid>[^/]+)/(?:task/[^/]+/)?(?P<leaf>{_PROC_LEAF})/*$")


def _proc_secret(path):
    """Whether path is a /proc entry that holds another process's environment, memory or open files (T-4736 review): any
    pid, a computed one too; `self` and `thread-self` only for environ (own memory and files are the caller's own)."""
    m = _PROC_SECRET.search(os.path.normpath(path)) if path.startswith("/proc/") else None
    return bool(m) and (m["leaf"] == "environ" or m["pid"] not in ("self", "thread-self"))


def classify_read(path, ctx):
    """T-1224: a read of the home folder's credential folders and files (their public parts aside: *.pub, ~/.ssh's
    known_hosts, authorized_keys and config, ~/.aws/config), where the path is and where it really is. A project's .env
    or *.key is the project's own: its reads stay ordinary. T-4736 review: and another process's /proc environ, mem,
    maps and fd entries (_proc_secret)."""
    if _proc_secret(path):
        return ["credentials"]
    for p in _variants(re.sub(r"^/+", "/", path)):
        low = p.lower()  # T-4667: any letter case
        if low.endswith(".pub") or os.path.basename(low) in _CRED_PUBLIC.get(
                os.path.relpath(os.path.dirname(low), ctx.home.lower()), ()):
            continue
        if _home_credential(p, ctx):
            return ["credentials"]
    return []


# T-1224: commands whose file arguments are read (a tool that only uses a key, ssh or ssh-add, is not a read of it)
_READERS = {"cat", "tac", "less", "more", "most", "head", "tail", "nl", "sort", "uniq", "cut", "paste", "fold", "rev",
            "cp", "scp", "rsync", "tar", "zip", "gzip", "bzip2", "xz", "zstd", "base64", "base32", "basenc", "xxd", "od",
            "hexdump", "strings", "bat", "batcat", "view", "vi", "vim", "nvim", "nano", "emacs", "diff", "cmp", "iconv",
            "grep", "egrep", "fgrep", "zgrep", "rg", "ag", "awk", "gawk", "mawk", "sed", "jq", "yq"}
_PATTERN_FIRST = {"grep", "egrep", "fgrep", "zgrep", "rg", "ag", "awk", "gawk", "mawk", "sed", "jq", "yq"}
_SENDERS = {"curl", "wget", "nc", "ncat", "netcat", "socat", "telnet"}
_SKIPS = ("--exclude", "--include", "--exclude-from", "--include-from")
_VALUE_OPTS = {"scp": ("-i", "-F", "-o", "-c", "-J", "-l", "-P", "-S", "-D", "-X") + _SKIPS,
               "rsync": ("-e", "--rsh", "-f", "--filter", "--files-from", "--log-file", "-T", "--temp-dir") + _SKIPS,
               "tar": ("-C", "--directory", "-X", "-T", "--files-from") + _SKIPS}
# security review: these options read the file they name (a list of names or patterns)
_LIST_OPTS = {"rsync": ("--files-from", "--exclude-from", "--include-from"),
              "tar": ("-X", "-T", "--files-from", "--exclude-from", "--include-from")}


def _link_sources(name, args):
    """T-4666: the files a hardlinking command gives a second name (ln and link without -s, cp -l / --link): the new name
    is one inode with the source, so a write to the released-looking name is a write to the source."""
    base = os.path.basename(name)
    if base not in ("ln", "link", "cp"):
        return []
    shorts = [a for a in args if a.startswith("-") and not a.startswith("--")]
    longs = [a.split("=", 1)[0] for a in args if a.startswith("--") and a != "--"]
    if base == "cp":
        hard = any("l" in a[1:] for a in shorts) or any(len(a) >= 3 and "--link".startswith(a) for a in longs)
    else:
        hard = not (any("s" in a[1:] for a in shorts) or any(len(a) >= 4 and "--symbolic".startswith(a) for a in longs))
    if not hard:
        return []
    pos, tdir = _positionals(args), _opt_values(args, "-t", "--target-directory")
    if tdir:
        return [p for p in pos if p not in tdir]
    return pos if len(pos) == 1 else pos[:-1]


def classify_hardlink(path, ctx):
    """T-4666: a source that is a credential, where it is and where it really is (a '..' after a link: the caller's
    _judge, T-4667)."""
    out = ["credentials"] if any(_is_credential(p, ctx) for p in _variants(path)) else []
    return out or classify_read(path, ctx)


def _read_targets(name, args):
    """The files a reading command reads: its operands (a grep-like's first one is its pattern, unless -e or -f gives
    it; tar's relative ones are under its -C folder), the lists tar and rsync read names from, and dd's if=."""
    if name == "dd":
        return [a[3:] for a in args if a.startswith("if=")]
    if re.fullmatch(_INTERP_NAMES, name):  # T-1585 review: perl -pe 1 FILE, python3 -c CODE FILE read their operands
        return [a for a in args if not a.startswith("-")] + [a.split("=", 1)[1] for a in args
                                                             if a.startswith("-") and "=" in a]  # --key=FILE
    if name not in _READERS:
        return []
    takes = _VALUE_OPTS.get(name, ())  # review: scp -i KEY uses the key; tar --exclude .ssh names what it skips
    pos, cdir = [], None
    for i, a in enumerate(args):
        if name == "tar" and (i and args[i - 1] in ("-C", "--directory") or a.startswith("--directory=")):
            cdir = a.split("=", 1)[1] if a.startswith("--directory=") else a
        elif not a.startswith("-") and not (i and args[i - 1] in takes):
            pos.append(a if cdir is None or a.startswith(("/", "~", "$")) else os.path.join(cdir, a))
    if name in _PATTERN_FIRST and not any(re.match(r"-[a-zA-Z]*[ef]|--(regexp|file|expression|from-file)", a)
                                          for a in args):
        pos = pos[1:]
    return pos + _opt_values(args, *_LIST_OPTS.get(name, ()))


def _sent_files(name, args):
    """T-1224: (files, reads stdin) a network sender attaches: curl's -d/--data*/--json @F, -F x=@F or x=<F and -T F;
    wget's --post-file/--body-file; nc, socat and telnet send their stdin."""
    if name in ("nc", "ncat", "netcat", "socat", "telnet"):
        return [], True
    if name == "wget":
        files = _opt_values(args, "--post-file", "--body-file")
    else:
        files = [v[1:] for v in _opt_values(args, "-d", "--data", "--data-binary", "--data-ascii", "--json")
                 if v.startswith("@")]
        files += [v.split("@", 1)[1] for v in _opt_values(args, "--data-urlencode") if "@" in v.split("=", 1)[0]]
        files += [m[1] for v in _opt_values(args, "-F", "--form") for m in [re.search(r"=[@<]([^;]*)", v)] if m]
        files += _opt_values(args, "-T", "--upload-file")
    stdin = [f for f in files if f in ("-", ".", "/dev/stdin")]
    return [f for f in files if f not in stdin], bool(stdin)


_ENVIRON = lazy(r"/proc/[^/\s]+/environ$")


def _prints_environment(c):
    """env, printenv (no names), set, export -p or declare -x print the whole environment; or a read of /proc/*/environ."""
    argv, _ = _strip_wrappers(c.argv)  # env CMD is a wrapper (it runs CMD); env with no command is left as nothing
    if not argv:
        return any(os.path.basename(a) == "env" for a in c.argv)
    name, args = os.path.basename(argv[0]), argv[1:]
    if any(_ENVIRON.search(a) for a in args):
        return True
    return (name == "printenv" and not _positionals(args) or name in ("set", "export", "declare", "typeset")
            and all(a in ("-p", "-x", "-px", "-xp") for a in args) and (name != "set" or not args))


# T-1224: chmod/chown/chgrp -R, or a mode that opens write to others, on / , the home folder or a system folder:
# folders always the system's (anything under them), and folders whose top only is (/var, /opt, /home: what's inside
# may be the user's). /dev, /proc and /sys count inside only recursively: chmod 666 /dev/ttyUSB0 is a dev box's chore
_SYS_TREES = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/boot", "/System", "/Library", "/private/etc")
_DEV_TREES = ("/dev", "/proc", "/sys")
_SYS_TOPS = ("/", "/var", "/opt", "/srv", "/home", "/root", "/Users", "/private", "/snap", "/run", "/mnt", "/media")
_MODE_OPT = lazy(r"--\S+|-[RfvchHLP]+")


def _opens_mode(mode, bits):
    """A mode that gives group or other any of `bits` ("w", or "wx"): 0775, g+w, o=rwx, +w (no who: all, less the
    umask). Taking them away (go-w) or setting them for the owner only (u+w, 0700) is no hazard."""
    if re.fullmatch(r"[0-7]{1,4}", mode):
        mask = (2 if "w" in bits else 0) | (1 if "x" in bits else 0)
        return any(int(d) & mask for d in mode.zfill(3)[-2:])
    for cl in mode.split(","):
        m = re.fullmatch(r"([ugoa]*)([+=])([rwxXst]*)", cl)
        if m and set(m[3]) & set(bits + ("X" if "x" in bits else "")) and (not m[1] or set(m[1]) & set("goa")):
            return True
    return False


def _mode_targets(name, args):
    """(targets, recursive) of a chmod/chown/chgrp that changes them recursively (chmod: opening write or exec to
    others) or opens write to others; ([], False) for any other."""
    if name not in ("chmod", "chown", "chgrp"):
        return [], False
    words = [a for a in args if not _MODE_OPT.fullmatch(a) and a != "--"]
    recursive = any(a.startswith("--rec") or re.fullmatch(r"-[RfvchHLP]*R[RfvchHLP]*", a) for a in args)
    if any(a.startswith("--ref") for a in args) or not words:
        return (words, True) if recursive else ([], False)
    if name != "chmod":
        return (words[1:], True) if recursive else ([], False)
    if _opens_mode(words[0], "wx" if recursive else "w"):
        return words[1:], recursive
    return [], False


def classify_system_mode(path, ctx, recursive=True):
    for p in _variants(re.sub(r"^/+", "/", os.path.normpath(path))):  # //etc is /etc; a link is where it points
        if _SAFE_DEV.match(p):  # /dev/null is 666 already
            continue
        if p in _SYS_TOPS + _DEV_TREES + (os.path.normpath(ctx.home),) or any(
                _under(p, t) for t in _SYS_TREES + (_DEV_TREES if recursive else ())):
            return ["system"]
    return []


# T-0340: where Codex, Gemini CLI and opencode load Foreman's hooks from (user-wide or a project's): editing one can
# switch the guard off in that agent, as Claude Code's settings can in Claude
_AGENT_WIRING = lazy(r"/\.codex/(?:hooks\.json|config\.toml)$|/\.gemini/settings\.json$|/opencode\.jsonc?$|"
                           r"/\.?opencode/plugins?(?:/|$)")


def _homes(ctx):
    """T-2967: the home as named and where it really is (after sampo migrate the old name is a symlink to the new):
    core is judged under both."""
    return list(dict.fromkeys([ctx.foreman_home, os.path.realpath(ctx.foreman_home)]))


def _is_core(path, ctx):
    """Protected core: all Sampo code (it enforces the guard), the rules, the eval suite, the spec and settings."""
    files = {os.path.join(fh, f) for fh in _homes(ctx)
             for f in ("plugin/rules/foreman.md", "BUILD_PROMPT.md") + _CORE_CONFIG}
    dirs = [os.path.join(fh, "plugin", d) for fh in _homes(ctx) for d in _CORE_DIRS] + \
        [os.path.join(fh, d) for fh in _homes(ctx) for d in _CORE_CONFIG_DIRS]
    codex = os.environ.get("CODEX_HOME")
    return path in files or any(_under(path, d) for d in dirs) or path == os.path.join(ctx.home, ".claude.json") or \
        bool(re.search(r"/\.claude/settings(\.local)?\.json$", path) or _AGENT_WIRING.search(path)) or \
        bool(codex) and path in (os.path.join(codex, "hooks.json"), os.path.join(codex, "config.toml")) or \
        path in _cargo_files(tuple(_homes(ctx)), ctx.home, os.environ.get("CARGO_HOME")) or \
        any(_under(path, d) for d in _rust_dirs(ctx.home))


# T-3607: what the installer makes in plugin/native (gitignored, so a swap leaves no diff to review)
_NATIVE_BUILT = ("sampo-fast", "sampo-fast.sha256", "sampo-fast.src", "target")
_NATIVE_TEMP = ".sampo-fast-"  # T-3608: the installer's staging files (.sampo-fast-new-…, .sampo-fast-sum-…)
# T-3607: what cargo and rustup read in every folder at or above the one they build in: a setting there (rustc-wrapper,
# linker, rustflags, target-dir, a toolchain path) reaches the binary hooks/fast runs
_CARGO_FILES = (".cargo", ".cargo/config", ".cargo/config.toml", "rust-toolchain", "rust-toolchain.toml")


@functools.lru_cache(maxsize=8)
def _cargo_files(homes, home, cargo_home):
    """T-3607: _CARGO_FILES at or above each home's plugin/native, and cargo's home config (~/.cargo and CARGO_HOME)."""
    out = {os.path.join(c, f) for c in (os.path.join(home, ".cargo"), cargo_home) if c for f in ("config", "config.toml")}
    for fh in homes:
        d, up = os.path.join(fh, "plugin", "native"), None
        while d != up:
            out.update(os.path.join(d, f) for f in _CARGO_FILES)
            d, up = os.path.dirname(d), d
    return frozenset(out)


def _rust_dirs(home):
    """T-3607 review: the toolchain cargo and rustup run: cargo's bin/ (the cargo and rustc proxies), rustup's
    toolchains/ and settings.toml (the default toolchain, each folder's override), at home and where the environment
    moves them. T-3608: and the crates the installer builds from, cargo's registry/ and git/ (a changed crate source
    is compiled into the binary)."""
    cargo = {os.path.join(home, ".cargo"), os.environ.get("CARGO_HOME")} - {None, ""}
    rustup = {os.path.join(home, ".rustup"), os.environ.get("RUSTUP_HOME")} - {None, ""}
    return [os.path.join(c, x) for c in cargo for x in ("bin", "registry", "git")] + [os.path.join(r, x) for r in rustup
                                                       for x in ("toolchains", "settings.toml")]


def _plain_path(detail):
    """T-0180: the one file a finding names, for a standing or trusted yes to cover: the path itself, or written from
    interpreter code; None for anything else (a tree write, a target not known before it runs, a relative path)."""
    if not isinstance(detail, str):
        return None
    path = detail.removesuffix(" (written from interpreter code)")
    return path if path.startswith("/") and " (" not in path else None


def _standing_covers(path, ctx):
    """A standing core yes (T-0119) covers Sampo's own code, rules, evals and spec; never this guard file, Claude
    Code's settings or Sampo state, which still ask each time."""
    path, homes = _plain_path(path), _homes(ctx)
    if path is None:
        return False
    real = os.path.realpath(path)  # the old home name is a symlink to the new one after a migration
    if any(_under(os.path.join(fh, "plugin", "lib", *g.split("/")), p) for fh in homes for g in GUARD_FILES
           for p in (path, real)):
        return False  # the guard itself, or a whole-tree write that includes it
    if any(_under(b, p) or _under(p, b) for fh in homes for b in (os.path.join(fh, "plugin", "native", x)
                                                                    for x in _NATIVE_BUILT) for p in (path, real)):
        return False  # T-3607: the binary hooks/fast runs, its sum or its build, or a tree write over one
    if any(os.path.dirname(p) == os.path.join(fh, "plugin", "native") and os.path.basename(p).startswith(_NATIVE_TEMP)
           for fh in homes for p in (path, real)):
        return False  # T-3608: a file the installer stages before it renames it over the binary
    if any(".cargo" in p.split("/") or os.path.basename(p).startswith("rust-toolchain") for p in (path, real)) or \
            path in _cargo_files(tuple(homes), ctx.home, os.environ.get("CARGO_HOME")):
        return False  # T-3607 review: cargo config and toolchain files change how plugin/native builds
    return real in {os.path.join(fh, f) for fh in homes for f in ("plugin/rules/foreman.md", "BUILD_PROMPT.md")} or any(
        _strictly_under(real, os.path.join(fh, "plugin", d)) for fh in homes for d in _CORE_DIRS)


def _trust_covers(path, ctx):
    """/fm-trust on (T-0120) covers every core file, the guard and Claude Code settings included; never Sampo state
    (only fm writes it) or a whole-tree write."""
    path = _plain_path(path)
    if path is None:
        return False
    states = ctx.state_dirs
    return not any(d and _under(path, d) for d in states)


_SHELL_RC = {".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile", ".zshrc", ".zprofile", ".zshenv",
             ".zlogin", ".zlogout", ".xprofile", ".xinitrc", ".xsessionrc", ".config/fish/config.fish"}
_LATER_DIRS = (".config/systemd", ".config/autostart", ".config/environment.d", ".config/fish/conf.d",
               ".config/fish/functions", ".bashrc.d", ".local/share/applications")


def _runs_later(p, ctx):
    """Code that runs later, outside the session: user units and autostart entries, shell startup files, git hooks."""
    home = ctx.home
    return (any(p == os.path.join(home, f) for f in _SHELL_RC) or any(_under(p, os.path.join(home, d))
                                                                      for d in _LATER_DIRS)
            or "/.git/hooks/" in p or _GIT_CONFIG_FILE.search(p) is not None
            or p in (os.path.join(home, ".gitconfig"), os.path.join(home, ".config", "git", "config")))


# T-1107: a git config file can set core.hooksPath, a `!` alias or a filter driver, as `git config` can (_git_config_code)
_GIT_CONFIG_FILE = lazy(r"/\.git/(?:worktrees/[^/]+/)?config(?:\.worktree)?$", re.I)  # I: macOS folds case


def _protected_roots(ctx):
    """(path, category) of everything the guard protects, for writes that cover a whole tree."""
    cl = os.path.join(ctx.home, ".claude")
    roots = [(os.path.join(fh, "plugin", d), "core") for fh in _homes(ctx) for d in _CORE_DIRS]
    roots += [(os.path.join(ctx.home, f), "core") for f in (".codex/hooks.json", ".codex/config.toml",  # T-0340
                                                             ".gemini/settings.json", ".config/opencode")]
    for fh in _homes(ctx):
        roots += [(os.path.join(fh, f), "core") for f in _CORE_CONFIG + _CORE_CONFIG_DIRS]
        roots += [(os.path.join(fh, "plugin", "rules", "foreman.md"), "core"), (os.path.join(fh, "BUILD_PROMPT.md"), "core")]
    roots += [(os.path.join(ctx.home, ".claude.json"), "core"), (os.path.join(cl, "plugins"), "plugin")]
    # user-wide Claude Code settings (a project's own are handled in classify_tree)
    roots += [(os.path.join(ctx.home, ".claude", f), "core") for f in ("settings.json", "settings.local.json")]
    # Foreman state inside a tree write: the user may approve it (core); a direct write never is (state-direct)
    roots += [(d, "core") for d in ctx.state_dirs]
    roots += [(p, "core") for p in _cargo_files(tuple(_homes(ctx)), ctx.home, os.environ.get("CARGO_HOME"))]  # T-3607
    roots += [(p, "core") for p in _rust_dirs(ctx.home)]
    roots += [(os.path.join(ctx.home, d), "credentials") for d in _CRED_DIRS + _CRED_FILES]
    return roots


def classify_tree(path, ctx):
    """A checkout, extraction or recursive copy at `path` can rewrite anything under it: the protected paths it
    contains, as categories (classify_write covers what `path` itself is under)."""
    cats = []
    for p in _variants(path):
        cats += [cat for root, cat in _protected_roots(ctx) if _under(os.path.normpath(root), p)]
        # this project's .claude/settings*.json, for writes aimed into the project (cp -r x .claude/), not a checkout
        # at its root: branch work in the user's own repo is ordinary
        pr = ctx.project_root
        if pr and _strictly_under(p, pr) and any(_under(os.path.join(pr, ".claude", f), p)
                                                  for f in ("settings.json", "settings.local.json")):
            cats.append("core")
    return list(dict.fromkeys(cats))


def _new_context_file(p, ctx):
    """Creating a skill, agent or command (user-wide or this project's) adds always-on context to every session there;
    editing one that exists is ordinary work. A whole folder (copied, moved, linked or extracted) counts too: aimed at
    the skills/agents/commands folder itself, or a new entry directly in it."""
    q = os.path.normpath(p)
    for d in (os.path.normpath(os.path.join(base, ".claude", kind)) for base in (ctx.home, ctx.project_root) if base
              for kind in ("skills", "agents", "commands")):
        if q == d or (_under(q, d) and not os.path.exists(q) and (q.endswith(".md") or os.path.dirname(q) == d)):
            return True
    return False


# T-1727 review: commands that may take a file away before a touch in the same command makes it again. ponytail: any of
# them in the command counts, not only one naming the touched path; match the path if that over-refuses
_REMOVERS = {"rm", "unlink", "mv", "shred", "rmdir", "find", "rsync", "git"}


def _classify_made(path, ctx):
    """T-1680: a touch writes only a file it makes. A dangling link doesn't exist to os.path.exists, so the file it
    points at is judged as made (classify_write follows it)."""
    return [] if os.path.exists(path) else classify_write(path, ctx)


def classify_unlink(path, ctx):
    """T-0412: rm of a symlink (no trailing slash) removes the link, never what it points at: judged by its own path."""
    return classify_write(path, ctx, real=not os.path.islink(path))


# T-1575: the machine's own folders; a write there changes the system for every user and session
_SYSTEM_DIRS = ("/etc", "/usr", "/boot", "/sys", "/proc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/opt",
                "/root", "/var", "/srv", "/snap")
_SYSTEM_OK = ("/var/tmp", "/var/folders", "/proc/self/fd", "/proc/thread-self/fd")


def _system_path(p, ctx):
    return (any(_under(p, d) for d in _SYSTEM_DIRS) and not any(_under(p, d) for d in _SYSTEM_OK)
            and not re.match(r"/proc/(?:self|thread-self|\d+)/fd(?:/|$)", p)  # /dev/stderr's real path
            and not _under(p, ctx.home)
            and not (ctx.project_root and os.path.exists(os.path.join(ctx.project_root, ".git"))  # cd /etc isn't a
                     and _under(p, ctx.project_root))  # project: only a real checkout under /opt or /srv is exempt
            # (exists, not isdir: a lane worktree's .git is a file, and every lane edit was refused, T-3890)
            and not any(_under(p, s) for s in getattr(ctx, "scratch", ()) or ()))


def _fm_mirror(d):
    """.sampo/ is fm's mirror only when it has a tasks/ folder or fm's README heading (T-3342)."""
    try:
        if os.path.isdir(os.path.join(d, "tasks")):
            return True
        with open(os.path.join(d, "README.md"), encoding="utf-8") as f:
            return f.readline().strip() == "# .sampo"
    except (OSError, ValueError):
        return not os.path.exists(d)  # absent: a first write is fm's own


def classify_write(path, ctx, real=True):
    cats = []
    # a link (real=False) is judged where it is, and where its folder really is (a link inside a linked folder)
    for p in (_variants(path) if real else {path, os.path.join(os.path.realpath(os.path.dirname(path)),
                                                                os.path.basename(path))}):
        mirrors = [os.path.join(ctx.project_root, d) for d in (".foreman", ".sampo")] if ctx.project_root else []  # fm sync
        mirrors = [m for m in mirrors if not m.endswith(".sampo") or _fm_mirror(m)]  # a user's own .sampo/ isn't state
        if any(d and _under(p, d) for d in [*ctx.state_dirs, *mirrors]):
            cats.append("state-direct")
        if _is_core(p, ctx):
            cats.append("core")
        if _is_credential(p, ctx):
            cats.append("credentials")
        if (p.startswith("/dev/") and not _SAFE_DEV.match(p)) or _runs_later(p, ctx) or _system_path(p, ctx):
            cats.append("system")  # device files; persistence that outlives the session; the system's folders
        if _under(p, os.path.join(ctx.home, ".claude", "plugins")) or _new_context_file(p, ctx):
            cats.append("plugin")  # installed plugins, or a new skill/agent/command: what runs in every session
        lane, main = getattr(ctx, "confine", None) or (None, None)
        rlane, rmain = getattr(ctx, "confine_real", None) or (lane, main)
        if lane and not _under(p, lane) and not _under(p, rlane) and not _under(os.path.realpath(p), rlane) \
                and not (p.startswith("/dev/") and _SAFE_DEV.match(p)) \
                and (any(_under(p, m) for m in (main, rmain) if m) or not any(_under(p, s) for s in ctx.scratch)):
            cats.append("confine")
        root = getattr(ctx, "unbriefed", None)  # strictly inside: a checkout-wide git write (merge, pull) is no edit
        if root and _strictly_under(p, root) and "state-direct" not in cats and not any(  # a scratch dir holding the
                _under(p, d) for d in [*ctx.scratch, os.path.join(ctx.home, ".claude", "projects")]  # project (/tmp)
                if not _under(root, d)) and not _in_briefed_lane(p, ctx):  # T-4340: this variant, as it is
            cats.append("brief")
    return list(dict.fromkeys(cats))  # a/link/../x: the caller judges where it really goes too (_walk, _judge: T-4667)


def _in_briefed_lane(p, ctx):
    """T-4340: whether path p lies strictly inside a lane the hook found its one task active in, outside any .git (the
    worktree's pointer file, and whatever a .git folder under it holds, stay refused)."""
    for d in getattr(ctx, "briefed_lanes", ()):
        d = d.rstrip("/")
        if _strictly_under(p, d) and ".git" not in p[len(d) + 1:].split("/"):
            return True
    return False


# ---------------------------------------------------------------- shell parsing

class Cmd:
    def __init__(self, argv, redirs, piped, procsub=False, op="", depth=0):
        self.argv, self.redirs, self.piped, self.procsub = argv, redirs, piped, procsub
        self.op = op  # T-0339: the separator before it (";", "&&", "|", …; "" first)
        self.depth = depth  # T-0414: the ( it sits inside, unclosed: a subshell, $( ) or <( ) (negative: a stray ))
        self.feeds = []  # T-0589: ("<<", delimiter) and ("<<<", word): what it reads on stdin
        self.stdin = None  # T-1129: the file a < (or <>) redirect gives it


_HEREDOC_START = r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1"


def _heredoc_starts(seen, line):
    """T-0178: the heredocs a line starts, only where a shell reads one: outside every quote, comments aside (seen: the
    command's lines before it, bodies left out). A quoted marker is text; taken for a start, it made every later line
    a body, so `echo '<<EOF'` hid the command after it from the guard."""
    if "<<" not in line:  # T-1778: no marker, nothing to read (the lines before were joined for every line: quadratic)
        return []
    before = "".join(x + "\n" for x in seen)
    return [(m.group(1), m.group(2)) for m in re.finditer(_HEREDOC_START, line)
            if _open_quote(before + line[:m.start()]) is None]


def _open_quote(prefix):
    """_inner_quote(_strip_comments(prefix)), remembered: a script's chunks and the tails inside them are read again and
    again, each time through the same prefixes (T-4758: the quote scan was the guard's largest single cost)."""
    return _pure(prefix, lambda p: _inner_quote(_strip_comments(p)), "oq")


def _inner_quote(prefix):
    """T-0669: the quote open in the innermost $( … ) or `…` where shell text ends. Each opens a fresh quoting context,
    so the "x <<EOF" in "$(echo "x <<EOF")" is quoted text, though a flat scan reads the outer string as closed there.
    A case arm's ) may close a context early: then more reads as quoted, never less as code (no line hides)."""
    st, esc, i = [["", None, 0]], False, 0  # [kind, quote, paren depth]
    while i < len(prefix):
        ch, top = prefix[i], st[-1]
        if esc:
            esc = False
        elif ch == "\\" and top[1] != "'":
            esc = True
        elif top[1] == "'":
            top[1] = None if ch == "'" else "'"
        elif ch == "`":
            if top[0] == "`" and top[1] is None:
                st.pop()
            else:
                st.append(["`", None, 0])
        elif ch == "$" and prefix[i + 1:i + 2] == "(":
            st.append(["$(", None, 0])
            i += 2
            continue
        elif top[1] == '"':
            top[1] = None if ch == '"' else '"'
        elif ch in "'\"":
            top[1] = ch
        elif ch == "(" and top[0] == "$(":
            top[2] += 1
        elif ch == ")" and top[0] == "$(":
            if top[2]:
                top[2] -= 1
            else:
                st.pop()
        i += 1
    return "\\" if esc else st[-1][1]


def _lines(text):
    """Newlines as command separators for the tokenizer, placed after each line so a # comment ends at its line (it ran
    to the end of the whole command, hiding every later line: T-0158). T-0669: not a newline inside quotes — that one
    is part of the word, and a second shell (eval 'a\\⏎b') must get it back as written."""
    mask = _mask_quotes(text)
    return "".join(ch + ";" if ch == "\n" and mask[i] == "\n" else ch for i, ch in enumerate(text))


def _ends_body(text, delim, starter):
    """Whether a line ends a heredoc body as bash reads it (T-0286 review): only the exact delimiter, leading tabs
    dropped for <<-. `X ` or ` X` is still body — taken for the end, the lines after it hid from the guard."""
    return text == delim or text.lstrip("\t") == delim and bool(
        re.search(r"<<-\s*(['\"]?)" + re.escape(delim) + r"\1", starter))


# T-0576: a plain expansion of IFS (default: space, tab, newline) reads as the space it splits on. T-0585 (its
# security review): only the plain forms; an operator form (${IFS:+word}) yields any word, and a line that may change
# IFS (named with quotes and backslashes removed, a loop variable, a nameref; IFS=… read scopes to that read) and
# splits anything on it can't be read that way
# T-1473: group 1 is the expansion; an odd run of backslashes before the $ makes it literal text (\$IFS), so it never
# expands, in or out of double quotes; an even run (\\$IFS) is escaped backslashes and then a real expansion
_IFS_PLAIN = lazy(r"(?<!\\)(?:\\\\)*(\$IFS(?![A-Za-z0-9_])|\$\{IFS\})")
_IFS_OP = lazy(r"\$\{[#!]?IFS(?![A-Za-z0-9_])")
_IFS_NAME = lazy(r"(?<![A-Za-z0-9_])IFS(?![A-Za-z0-9_])")
_IFS_IN_VALUE = lazy(r"(?<![\w$])[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=[^\s;&|()<>]*(?:\$IFS(?![A-Za-z0-9_])|\$\{IFS\})")
_IFS_FOR_READ = lazy(r"(?<![\w$])IFS=(?:\$?'[^'\n]*'|\"[^\"\n]*\"|[^\s;&|'\"])*[ \t]+read\b")  # one line
_READ_REDEFINED = lazy(r"\b(?:alias|enable|function)\b|\bread\s*\(")
_NAME_COMPUTED = lazy(  # a variable name built by an expansion: declare $'\x49FS=/', export "$n=/", (( $n = 1 ))
    r"(?:^|[;&|({`\n]|\$\(|\b(?:then|do|else|elif)\s)\s*(?:builtin\s+|command\s+)?(?:"  # at command position
    # T-3799: a redirection's target (read n <<< "$a", read l < "$f") is input, never a name the builtin sets
    r"(?:declare|typeset|local|export|readonly|read|mapfile|readarray|getopts|let)\b[^;&|\n]*?(?<![<>])\s(?![<>])"
    r"[^\s=;&|<>]*[$`'\"\\]"
    r"|(?:declare|typeset|local)\b[^;&|\n]*\s-\w*n"
    # T-3223: printf's -v is an option only before its format: never past a quote or an expansion (command -v inside
    # "$(…)" among its arguments isn't one); a first word an expansion, brace or backtick could make -v counts too
    r"|printf\b[^;&|\n$`'\"]*[\"']?-v[\"']?\s*\S*[$`'\"\\]|printf\s+[^\s;&|]*[$`{])"
    r"|\(\(\s*(?:[^()]*,\s*)?\$\{?\w+\}?\s*(?:[-+*/%^|&]|<<|>>)?=(?!=)|\$\{![^}]*=")  # (( $n = 1 )), not (( a == b ))
_PLAIN_QUOTED = lazy(r"(['\"])([^\s'\"`$\\;&|()<>{}#]+)\1")


def _name_computed(text):
    """_NAME_COMPUTED on text with its quoted data masked (a quote after declare may build a name), or with each quoted
    plain word ("-v", 'x') read as bash reads it, its quotes removed. T-3799: a line with a $( ) inside double quotes
    used to stay unmasked, which alone kept printf "-v" "$(…)" visible; masked, its -v would hide."""
    return _NAME_COMPUTED.search(_mask_quotes(text)) or _NAME_COMPUTED.search(_mask_quotes(_PLAIN_QUOTED.sub(r"\2", text)))


_UNQUOTED = lazy(r"\$[\w{@*#?!$(-]|`")
_RUNS_HERE = lazy(  # T-0590: code this shell runs that the line doesn't show: eval text, a sourced file
    # T-1473 review: after any keyword, !, a case pattern's ) or prefix assignments too (if eval, a=b . f)
    r"(?:^|[;&|({`\n)!]|\$\(|\b(?:then|do|else|elif|if|while|until|coproc|time(?:\s+-p)?)\s)\s*(?:\w+=\S*\s+)*"
    r"(?:builtin\s+|command\s+)?(?:eval|source|\.)(?=\s)")
# an unquoted word that runs a string again in this shell (eval, source, trap, mapfile -C, alias), anywhere on the line
_EVAL_WORD = lazy(r"(?<![\w.$/-])(?:eval|source|trap|mapfile|readarray|alias)(?![\w.=/-])")


def _ifs_unreadable(cmd):
    """T-0585: why bash's word splitting on this line can't be read (IFS may not be the default), or None."""
    rest = _IFS_PLAIN.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)] + " ", cmd)
    if _IFS_OP.search(rest):
        return "IFS is expanded with an operator: the guard can't read the word it yields"
    if _IFS_IN_VALUE.search(_mask_quotes(cmd)):  # T-0592: bash keeps it whole there; a space would end the value
        return "IFS in an assignment's value: the guard can't read where that value is split later"
    why = "IFS may be changed on a line that splits words on it: the guard can't read how bash splits them"
    plain = rest != cmd
    hidden = _RUNS_HERE.search(rest) or _EVAL_WORD.search(_mask_quotes(_strip_heredocs(rest)))
    data = _ifs_data(cmd) if plain else None
    if data is not None:  # T-0698: IFS only in data, unless eval or source beside it may run it here
        return why if _RUNS_HERE.search(data) or _name_computed(_strip_heredocs(data)) else None
    if plain and hidden:  # T-0590: eval or source may set IFS out of sight
        return why
    scoped = rest if _READ_REDEFINED.search(rest) else _IFS_FOR_READ.sub(" read", rest)
    # quoted text and heredoc bodies are data: check_bash reads what eval, bash -c and bash <<EOF run on their own;
    # beside a plain use or an eval/source they aren't (T-0590: eval 'IFS=m'; "r$IFS" -rf ~)
    bare = _mask_quotes(_strip_heredocs(scoped))
    text = scoped if plain or hidden else bare
    if _IFS_NAME.search(re.sub(r"[\\'\"]", "", text)):  # I""FS and I\FS are IFS to bash
        # T-1473: \$ and \` are literal characters, except in text eval or source runs again (review: eval "…\$IFS…")
        live = text if hidden else re.sub(r"\\.", "", text)
        split = plain or "$" in live or "`" in live
    elif _name_computed(_strip_heredocs(scoped)):  # ponytail: a computed name is refused only beside an expansion that splits
        split = plain or _UNQUOTED.search(bare)
    else:
        return None
    return why if split else None


def _ifs_data(cmd):
    """T-0698: cmd with one piece of data blanked when every IFS on the line, plain or named, is in it: a '…' string
    or the body of a quoted-delimiter heredoc no shell on the line reads. Nothing in it expands, and a shell that runs
    it (eval, bash -c) reads it whole on its own. None when IFS is also beside it or in a second piece (bash -c
    'IFS=/ source /dev/stdin' <<< '…${IFS}…'), or where quotes aren't read flat (`…`, and what _mask_quotes leaves)."""
    lines, bodies, seen, i = cmd.split("\n"), [], [], 0
    offs = [0]
    for line in lines:
        offs.append(offs[-1] + len(line) + 1)
    while i < len(lines):
        line, i = lines[i], i + 1
        starts = _heredoc_starts(seen, line)
        seen.append(line)
        for quote, delim in starts:
            first = i
            while i < len(lines) and not _ends_body(lines[i], delim, line):
                i += 1
            bodies.append((offs[first], offs[i], quote))
            i += 1
    code = cmd
    for s, e, _ in bodies:
        code = code[:s] + re.sub(r"[^\n]", " ", code[s:e]) + code[e:]
    fed = any(q for *_, q in bodies) and _stdin_scripts(cmd, _split(_tokens(_lines("\n".join(seen)))))
    mask, pieces = "" if "`" in code else _mask_quotes(code), set()  # same length; quoted text as underscores
    for m in _IFS_PLAIN.finditer(cmd):
        p = m.start(1)
        body = next((b for b in bodies if b[0] <= p < b[1]), None)
        if body:
            if not body[2] or fed:  # <<E bodies expand; a shell's are code (T-0589)
                return None
            pieces.add(body[:2])
            continue
        o, c = max(mask.rfind("'", 0, p), mask.rfind('"', 0, p)), mask.find("'", p)
        if mask[o:o + 1] != "'" or c < 0 or set(mask[o + 1:c]) != {"_"}:  # "…" expands; code; a comment
            return None
        pieces.add((o + 1, c))
    if len(pieces) != 1:
        return None
    (s, e), = pieces
    out = cmd[:s] + re.sub(r"[^\n]", " ", cmd[s:e]) + cmd[e:]
    return None if _IFS_NAME.search(re.sub(r"[\\'\"]", "", out)) else out


def _ifs_spaces(cmd):
    """T-0576: an unquoted plain IFS expansion as the space bash splits on. T-0590: a # right after it stays a
    literal, not a comment; quoted text is left as it is for the nested check that reads it (bash -c '…')."""
    out, last = [], 0
    for m in _IFS_PLAIN.finditer(_mask_quotes(cmd)):  # same length as cmd, quoted text blanked
        out += [cmd[last:m.start(1)], " \\" if cmd[m.end():m.end() + 1] == "#" else " "]
        last = m.end()
    return "".join(out) + cmd[last:]


def _join_continued(cmd):
    """Lines as bash reads a command (T-0286 review): one ending in an unescaped backslash outside quotes and comments
    is joined with the next before heredoc bodies are read, so `python3 - <<'X' \\` + `1>FILE` is a redirect on the
    command line, not the first body line. Bodies are kept as they are."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        # T-1778: only a line ending in \ can go on (a comment never starts just after one), so the lines before are
        # read only then: reading them for every line was quadratic
        while i < len(lines) and line.endswith("\\"):
            code = _strip_comments("\n".join(out + [line]))
            tail = len(code) - len(code.rstrip("\\"))
            if not (code.endswith("\\") and tail % 2 and _quote_at(code[:-1]) in (None, '"')):  # T-0669: in "…" too
                break
            line = line[:-1] + lines[i]
            i += 1
        starts = _heredoc_starts(out, line)
        out.append(line)
        for _, delim in starts:
            while i < len(lines) and not _ends_body(lines[i], delim, line):
                out.append(lines[i])
                i += 1
            if i < len(lines):
                out.append(lines[i])
                i += 1
    return "\n".join(out)


def _live_heredocs(cmd):
    """The bodies of heredocs with an unquoted delimiter: a shell runs their substitutions (T-0158)."""
    lines, out, seen, i = cmd.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        for quote, delim in _heredoc_starts(seen, line):
            body = []
            while i < len(lines) and not _ends_body(lines[i], delim, line):
                body.append(lines[i])
                i += 1
            i += 1
            if not quote:
                out.append("\n".join(body))
        seen.append(line)
    return out


def _heredoc_split(cmd):
    """(the command's lines without heredoc bodies, each body's lines in order)"""
    lines, out, bodies, i = cmd.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        starts = _heredoc_starts(out, line)
        out.append(line)
        for _, delim in starts:
            bodies.append([])
            while i < len(lines) and not _ends_body(lines[i], delim, line):
                bodies[-1].append(lines[i])
                i += 1
            i += 1
    return out, bodies


def _heredocs(cmd):
    """(the command without its heredoc bodies, the bodies)"""
    out, bodies = _heredoc_split(cmd)
    return "\n".join(out), "\n".join(x for b in bodies for x in b)


# T-0589: what may read a heredoc or here-string as code (bash, sh -s, bash /dev/stdin, { bash; }, . /dev/stdin …)
_RUNS_STDIN = lazy(r"(ba|z|da|k|fi|c|tc|a|mk|ya|po|lk)?sh|busybox|source|\.|eval")
_STDIN_PATHS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
_INLINE = {"sh": "c", "py": "cm", "js": "ep", "pl": "eE", "rb": "e", "php": "rBRFE"}  # flags whose next word is the code
# T-0716: options whose next word is a value, not the script (bash -o posix, python3 -W ignore, node -r ./hook.js)
_OPT_VALUE = {"sh": "oO", "py": "WXQ", "js": "rC", "pl": "IMm", "rb": "IrCEF", "php": "cdz"}
_LONG_VALUE = {"--rcfile", "--init-file", "--require", "--import", "--loader", "--experimental-loader", "--input-type",
               "--conditions", "--title", "--env-file"}
# inline code that runs what it reads on stdin (python3 -c 'exec(sys.stdin.read())', perl -e 'eval <STDIN>');
# json.load, re.compile and ast.literal_eval read data
_RUNS_IT = lazy(r"(?<![.\w'\"])(?:eval|compile)\b|(?<!['\"])\b(?:exec|execSync|execFile\w*|execv\w*|execl\w*|"
                      r"Function|runpy|run(?:In\w*Context)|system|popen|Popen|subprocess|spawn\w*|instance_eval|"
                      r"class_eval)\b(?!['\"])")  # a quoted word ('system' in rows) names it, doesn't call it
# T-3608 round 7: the same for code that names an installer file (_code_runs, _starts_installer), where a miss runs the
# build: a call to a word holding exec, spawn, system or popen (create_subprocess_exec(, posix_spawn(), the word
# subprocess, and the ways to import and run a file. T-4827: no quote lookaround (the body must name the installer
# anyway, and getattr(os,'system')('bash install.sh') quotes the word it calls) and call shapes beside the bare
# words (_RUNS_IT_WORDS): filesystem, systemd or executable as plain words start nothing, but letting them through
# waits on the user's core yes, so the shapes add blocks and the plain-word notes below say what they alone pass. The
# shapes: the word and a '(' (or the bare word exec, system, spawn, popen and Popen before a space and a '(' or a
# quote, $, @, % or { for the perl and ruby calls without parens), a quoted word and a ] or ) and a '(' (getattr
# and a __dict__ lookup), getattr itself, and an import of one (from os import system as s). Not for
# _inline_runs_stdin or the piped-stdin gate: there json.load(sys.stdin) through __import__('json'), or
# importlib.metadata, reads data and a wider list refuses it for no reason.
# Round 1 review: the word subprocess anywhere in a word (create_subprocess_shell), a call word of os, posix, pty,
# shutil or subprocess named without a call (f = os.system; map(os.system, …)), whitespace before the '(' for every
# variant (os.execv (…)), and perl and ruby calls without parens (system qw(…), system q(…), system cmd). A prose
# "system (" or "the system cmd;" still counts: the file is named and a word reads as a call, which is acceptable.
# Round 2: a call word after any dot, not only os. and its kin (import os as o; o.system, libc.system,
# sys.modules['os'].system); popen2/popen3 and execa before a quote or a backtick (Open3.popen3 "…", execa`…`); and
# ruby's paren-less call with any name, a dotted one, a comma or a rescue after it (system CMD, system cmd, x: 1,
# system cmd.join(' '), system cmd rescue nil). Prose like "on the system and" still passes: no ending follows.
# Round 3: the literal subprocess (an unanchored \w*subprocess\w* is quadratic on one long word, a heredoc's base64
# line), a space after the dot (os. system) and Popen there, and an import list in parens over several lines.
# Round 4: linear on a word full of call words too ("system"*30000): a lookahead finds the word, then \w+ takes it
# once; an import of a call word is read clause by clause (_IMPORT_CLAUSE, round 5), not by this pattern.
_RUNS_IT_INSTALLER = lazy(r"(?<![.\w'\"])(?:eval|compile)\b|subprocess|\bchild_process\b|\bgetattr\s*\(|"
                          r"\.[ \t]*\w*(?:system|popen|Popen|exec|spawn)\w*\b|"
                          r"\b(?=\w*(?:exec|spawn|system|popen|Popen))\w+\s*\(|"
                          r"\b(?:exec|execa|system|spawn|popen\d?|Popen\d?)\s*['\"`$@%{\[]|"
                          r"\b(?:exec|system|spawn|popen)\s+(?:q[qw]?\W|[A-Za-z_][\w.]*\s*(?:[;),(\n]|\brescue\b|$))|"
                          r"['\"](?=\w*(?:exec|spawn|system|popen|Popen))\w*['\"]\s*[\])]\s*\(|"
                          r"\b(?:Function|runpy|run(?:In\w*Context)|instance_eval|class_eval|import_module|__import__|"
                          r"importlib|run_path|run_module|spec_from_file_location|load_module)\b")
# Round 5: an import of a call word (from os import system as s; from os import (…, system)) is found clause by
# clause, not by one pattern: finditer reads each clause once (a list in parens to its ')', else to the line's end),
# so neither padding before the word (a 500-character bound let it through) nor many unclosed lists cost more than
# one pass. Round 6: as Python reads it, a list in parens ends at a ')' outside a comment ("# note )" doesn't end it),
# and a plain one runs on after a backslash-newline; both alternations are disjoint, so the read stays one pass.
_IMPORT_CLAUSE = lazy(r"\bfrom\s+[\w.]+\s+import(?:\s*\([^)#]*(?:#[^\n]*\n?[^)#]*)*\)?|\s(?:[^\n;\\]|\\(?:\r\n|[\s\S]))*)")
_CALL_WORD = lazy(r"exec|spawn|system|popen|Popen")


# T-4827: the earlier bare-word rule, kept beside the call shapes: a word holding a call word, not quoted (filesystem,
# systemd and executable in prose count too). Letting those plain words through makes the guard block less, which
# only the user's core yes can pass (selfgate); until then this rule stands, so T-4827 adds blocks and removes none.
# A lookahead form of the old \w*(…)\w* (each word read once, linear on a word full of call words), blocking at least
# what it blocked.
_RUNS_IT_WORDS = lazy(r"(?<!['\"])\b(?=\w*(?:exec|spawn|subprocess|system|popen|Popen))\w+(?!['\"])")


def _runs_it_installer(code):
    """T-4827: whether inline code or a heredoc body calls something that may start the installer it names: a call
    shape (_RUNS_IT_INSTALLER), an import of a call word, or (until the user's core yes lets plain words through) an
    unquoted word holding one (_RUNS_IT_WORDS)."""
    return bool(_RUNS_IT_INSTALLER.search(code)
                or any(_CALL_WORD.search(m.group(0)) for m in _IMPORT_CLAUSE.finditer(code))
                or _RUNS_IT_WORDS.search(code))
# T-4827: one plain sentence, built by message() for this finding: what is refused, and its one way through
_INSTALLER_CORE = ("sampo doctor --repair|--fix, fastbuild.py and install.sh build and install the binary the "
                   "guard's hooks run (plugin/native/sampo-fast)")
_INSTALLER_SAYS = ("Ward: blocked core: sampo doctor --repair, sampo doctor --fix, fastbuild.py and install.sh build and "
                   "install the binary the guard's hooks run, so they need the user's core yes for this task, and "
                   "only that (trust and the standing yes don't cover a build): sampo ask {id} core --why "
                   "\"<what and why>\", then ask the user one yes/no question.")
_STDIN_WORD = lazy(r"stdin|STDIN|<>|readFileSync\(\s*0|/dev/fd/0|php://stdin")
_CAT_SUBST = lazy(r"\$\(\s*(?:cat\b[^)]*|<\s*/dev/stdin\s*)\)|`\s*cat\b[^`]*`")
_READ_RUNS = lazy(r"(?:^|[;&|({]|\bdo|\bthen|\belse)\s*(?:eval\b|\"?\$|\S*sh\s+-\w*c\s+\"?\$)")  # read l; $l


def _reads_stdin_code(name, args, via_xargs=False, any_word=False):
    """T-0715: whether a shell or interpreter reads its program from stdin: no script file and no inline code (a pipe
    into it then runs whatever the pipe carries; `curl … | python3 -c '…'` reads data, `| python3` reads code).
    T-0716: an option's value isn't the script, -s reads stdin whatever follows, a word that doesn't look like a path
    isn't taken for a script, and inline code that runs its stdin (or xargs' item) counts."""
    if name in ("source", "."):
        return bool(args) and args[0] in _STDIN_PATHS
    if re.fullmatch(r"[gmn]?awk", name):  # awk '{system($0)}', print | "sh": each input line run as a command
        return any(re.search(r"\bsystem\s*\(|\|\s*\"[^\"]*sh\b", a) for a in args)
    kind = ("sh" if re.fullmatch(r"(ba|z|da|k|fi|c|tc|a|mk|ya|po|lk)?sh|busybox", name) else
            "py" if re.fullmatch(r"python[0-9.]*|pypy[0-9.]*", name) else "js" if name in ("node", "nodejs", "deno", "bun")
            else "pl" if name == "perl" else "rb" if name == "ruby" else "php" if name == "php" else None)
    if kind is None:
        return False
    i, applet = 0, name != "busybox"
    while i < len(args):
        a = args[i]
        i += 1
        if a in _STDIN_PATHS:
            return True
        if a == "--":
            continue
        if a.startswith("--"):
            if kind == "js" and a in ("--eval", "--print") or kind == "sh" and a == "--command":
                return _inline_runs_stdin(kind, args[i] if i < len(args) else None, via_xargs)
            i += a in _LONG_VALUE
            continue
        if a.startswith(("-", "+")) and len(a) > 1:
            for j, f in enumerate(a[1:]):
                if f in _INLINE[kind] and not (kind == "py" and f == "m"):
                    code = a[j + 2:] or (args[i] if i < len(args) else None)
                    loops = kind in ("pl", "rb") and re.search(r"[np]", a[1:j + 1])  # perl -ne: $_ is each stdin line
                    return _inline_runs_stdin(kind, code if kind != "sh" else (args[i] if i < len(args) else None),
                                              via_xargs, loops)
                if kind == "py" and f == "m":
                    return False  # a module reads stdin as data (python3 -m json.tool)
                if kind == "sh" and f == "s" and a[0] == "-":
                    return True  # sh -s ARGS: the arguments are $1…, the script is stdin
                if f in _OPT_VALUE[kind]:
                    i += j == len(a) - 2  # -o VALUE; -Wignore holds its own
                    break
            continue
        if not applet:
            if a not in ("sh", "ash"):
                return False  # another busybox applet
            applet = True
            continue
        return not (any_word or re.search(r"[/.]", a))  # a script file; a bare word (bash posix) isn't clearly one
    return True


def _inline_runs_stdin(kind, code, via_xargs, loops=False):
    """Whether inline code (sh -c CODE, python3 -c CODE …) runs what it reads on stdin."""
    if code is None or via_xargs and code.strip() in ("{}", '"$@"', "$@", "$*", "$1"):
        return True  # no code here: xargs supplies it from the pipe
    if kind != "sh":
        return bool(_RUNS_IT.search(code) and (loops or _STDIN_WORD.search(code)))
    if _CAT_SUBST.search(code) and re.search(r"\beval\b|\b(?:ba|z|da|k)?sh\s+-\w*c", code):
        return True  # eval "$(cat)"
    if re.search(r"\bread\b", code) and _READ_RUNS.search(code):
        return True  # while read l; do $l; done
    for x in _split(_tokens(code)):
        argv, inner = _strip_wrappers(x.argv)
        if argv and not x.piped and not x.feeds and _reads_stdin_code(os.path.basename(argv[0]), argv[1:], inner):
            return True  # exec bash, source /dev/stdin: it inherits the pipe
    return False


def _stdin_scripts(cmd, cmds):
    """T-0589: the heredoc bodies and here-string words on a line that also runs a shell, source or eval: which of
    them a shell reads as its script isn't modelled (its review: /dev/stdin, a group, -c 'source /dev/stdin'), so
    all of them, read as commands"""
    feeds = [f for x in cmds for f in x.feeds]

    def reads(x):  # T-0714 (JARVIS): `. .venv/bin/activate` or `bash ./run.sh` runs a file, not its stdin
        argv, xargs = _strip_wrappers(x.argv)
        name = os.path.basename(argv[0]) if argv else ""
        return name == "eval" or bool(_RUNS_STDIN.fullmatch(name)) and _reads_stdin_code(name, argv[1:], xargs)
    if not feeds or not any(reads(x) or x.feeds and re.search(r"[$`]", _name(x.argv))
                            for x in cmds):  # T-0669: $X <<EOF, when that $X is the one fed
        return []
    return [w for op, w in feeds if op == "<<<"] + (["\n".join(b) for b in _heredoc_split(cmd)[1]]
                                                    if any(op == "<<" for op, _ in feeds) else [])


def _strip_heredocs(cmd):
    return _heredocs(cmd)[0]


def _drop_data_heredocs(cmd, only=None):
    """T-0171: the command without the bodies of heredocs that only write text: cat or tee as the line's first word,
    a quoted delimiter (nothing in the body expands) and no pipe or substitution on the line. For the interpreter gate
    only: a script written with cat isn't run by writing it. Anything else keeps its body, and so does a data
    heredoc whose line `only` (when given) rejects."""
    lines, out, seen, i = cmd.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        starts = _heredoc_starts(seen, line)
        out.append(line)
        seen.append(line)
        for quote, delim in starts:
            # T-1141: fm edit reads its heredoc as edit blocks it applies through the guard's own Edit check, never runs it
            data = bool(quote) and not re.search(r"[|`;&]|\$\(", line) and (
                line.split()[:2] in [[p, "edit"] for p in FM_PROGRAMS]
                or line.split()[:1] in (["cat"], ["tee"]) and (only is None or only(line)))
            while i < len(lines) and not _ends_body(lines[i], delim, line):
                if not data:
                    out.append(lines[i])
                i += 1
            if i < len(lines):
                out.append(lines[i])
                i += 1
    return "\n".join(out)


@functools.lru_cache(maxsize=256)  # T-1778: text in, text out; the same text is stripped by many checks
def _strip_comments(text):
    """T-0161 review: a # starts a comment only at the start of a word outside quotes; shlex's own commenters also cut
    at the # in `echo a#b; rm …` and hid the rest of the line. Under $'…' quoting nothing is cut (fails closed).
    T-1561 re-review: quoting as _inner_quote reads it, each $( … ) and `…` a fresh context: a flat scan read the inner
    " of "$(echo "a #" )">~/.bashrc as closing the string and cut the redirect away as a comment."""
    if "$'" in text:
        return text
    st, esc, out, i = [["", None, 0]], False, [], 0  # [kind, quote, paren depth]
    while i < len(text):
        ch, top = text[i], st[-1]
        if esc:
            esc = False
        elif ch == "\\" and top[1] != "'":
            esc = True
        elif top[1] == "'":
            top[1] = None if ch == "'" else "'"
        elif ch == "`":
            if top[0] == "`" and top[1] is None:
                st.pop()
            else:
                st.append(["`", None, 0])
        elif ch == "$" and text[i + 1:i + 2] == "(":
            st.append(["$(", None, 0])
            out.append("$(")
            i += 2
            continue
        elif top[1] == '"':
            top[1] = None if ch == '"' else '"'
        elif ch in "'\"":
            top[1] = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|(<>"):  # after ) it may end a $(…) word (T-0163)
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        elif ch == "(" and top[0] == "$(":
            top[2] += 1
        elif ch == ")" and top[0] == "$(":
            if top[2]:
                top[2] -= 1
            else:
                st.pop()
        out.append(ch)
        i += 1
    return "".join(out)


_ANSI_C = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
           "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c_body(text, i):
    """(value, index past its closing quote) of the $'…' body starting at text[i], decoded as bash does; None unclosed."""
    out = []
    while i < len(text):
        ch, e = text[i], text[i + 1:i + 2]
        if ch == "'":
            return "".join(out).split("\0")[0], i + 1  # bash's value ends at a NUL
        if ch != "\\" or not e:
            out.append(ch)
            i += 1
        elif e in _ANSI_C:
            out.append(_ANSI_C[e])
            i += 2
        elif e in "01234567":
            m = re.match(r"[0-7]{1,3}", text[i + 1:i + 4])  # bounded slices: a long run of escapes stays linear
            out.append(chr(int(m.group(), 8) & 0xff))
            i += 1 + len(m.group())
        elif e in "xuU" and re.match(r"[0-9a-fA-F]", text[i + 2:i + 3]):
            m = re.match(r"[0-9a-fA-F]{1,%d}" % {"x": 2, "u": 4, "U": 8}[e], text[i + 2:i + 10])
            n = int(m.group(), 16)
            out.append(chr(n) if n <= 0x10FFFF and not 0xD800 <= n <= 0xDFFF else "�")
            i += 2 + len(m.group())
        elif e == "c" and text[i + 2:i + 3] not in ("", "'"):
            out.append(chr(ord(text[i + 2]) & 0x1f))
            i += 3
        else:  # an escape bash doesn't know keeps its backslash
            out.append(text[i:i + 2])
            i += 2
    return None


def _decode_ansi_c(text):
    """T-1480: every $'…' outside quotes as the '…' literal bash makes of it ($'c\\x61t' is 'cat'; shlex read $'cat' as
    the computed name $cat). Quoting as _inner_quote reads it (each $( … ) and `…` opens a fresh context; review: a
    flat scan lost its place in "$(echo 'a"b')"), comments skipped, and heredoc bodies kept as written: bash doesn't
    decode $'…' there, and a body runs (bash <<EOF) only as a script of its own, which check_bash reads and decodes.
    An unclosed $'… is left as it stands from there on."""
    if "$'" not in text:
        return text
    st, esc, i, n, res, cur, seen = [["", None, 0]], False, 0, len(text), [], [], []  # st: [kind, quote, paren depth]
    while i < n:
        ch, top = text[i], st[-1]
        if esc:
            esc = False
        elif ch == "\\" and top[1] != "'":
            esc = True
        elif top[1] == "'":
            top[1] = None if ch == "'" else "'"
        elif top[1] is None and text.startswith("$$", i):  # $$'x' is the pid, then 'x'
            cur.append("$$")
            i += 2
            continue
        elif top[1] is None and text.startswith("$'", i):
            body = _ansi_c_body(text, i + 2)
            if body is None:
                cur.append(text[i:])
                break
            val, i = body
            cur.append("'" + val.replace("'", "'\\''") + "'")
            continue
        elif top[1] is None and ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|(<>"):
            end = text.find("\n", i)
            end = n if end < 0 else end
            cur.append(text[i:end])
            i = end
            continue
        elif ch == "`":
            if top[0] == "`" and top[1] is None:
                st.pop()
            else:
                st.append(["`", None, 0])
        elif ch == "$" and text[i + 1:i + 2] == "(":
            st.append(["$(", None, 0])
            cur.append("$(")
            i += 2
            continue
        elif top[1] == '"':
            top[1] = None if ch == '"' else '"'
        elif ch == "\n":  # a line ends outside quotes: the bodies of the heredocs it starts follow, kept as written
            line = "".join(cur)
            res.append(line + "\n")
            cur, i = [], i + 1
            for _, delim in _heredoc_starts(seen, line) if "<<" in line else []:
                while i < n:
                    end = text.find("\n", i)
                    end = n if end < 0 else end + 1
                    res.append(text[i:end])
                    i, body_line = end, text[i:end].rstrip("\n")
                    if _ends_body(body_line, delim, line):
                        break
            seen.append(line)
            continue
        elif ch in "'\"":
            top[1] = ch
        elif ch == "(" and top[0] == "$(":
            top[2] += 1
        elif ch == ")" and top[0] == "$(":
            if top[2]:
                top[2] -= 1
            else:
                st.pop()
        cur.append(ch)
        i += 1
    return "".join(res) + "".join(cur)


class _Quoted(str):
    """T-1561 review: a word of quoted or escaped operator characters ('>', \\;): data to bash, never a redirect or a
    separator. _split reads it as a word; it still equals its text (find -exec … ';' ends at it)."""


_OPS = ";&|()<>"
# what shlex mustn't see as itself, as plain word characters: _ESC and a digit is _ENCODED's character at that place,
# _ESC _ESC is _ESC (so no character a line already holds can pass for one: self-review)
_ESC, _ENCODED = "", _OPS + '"'


def _op_word(w):
    """[;&|()<>]+, a run of operator characters. T-3692: this and _decode test strings, so a fresh hook process reading
    a command compiles no regex (it compiled nine)."""
    return bool(w) and not w.strip(_OPS)


def _decode(token):
    """Each _ESC and the character after it back to what it stands for, left to right (as re.sub(_ESC + '(.)') did)."""
    out, i = [], 0
    while 0 <= (j := token.find(_ESC, i)) < len(token) - 1:
        c = token[j + 1]
        out += [token[i:j], _ESC if c == _ESC else _ENCODED[int(c)]]
        i = j + 2
    return "".join(out) + token[i:]


def _hide_quoted_ops(text, encoded=False):
    """(text with each operator character inside quotes or after a backslash encoded, what closes it: an unpaired
    trailing \\ doubled, then the quote left open): shlex drops the quotes, so a quoted '>' came out as the bare redirect
    >. Quoting as shlex reads it. encoded: the text's _ESC pairs are encoded already (_mask_nested_dq). T-2629: an
    unquoted fd number glued to < or > at a word's start is dropped, as bash reads it as the fd (2>/dev/null is no
    word 2: shlex split it off, and cp a b 2>/dev/null read ./2 as cp's destination)."""
    hide = lambda c: _ESC + str(_ENCODED.index(c)) if c and c in _OPS else c  # noqa: E731
    out, q, i, tail = [], None, 0, ""
    while i < len(text):
        ch = text[i]
        if not q and ch in "0123456789" and (i == 0 or text[i - 1] in " \t\n;&|("):
            j = i + 1  # [0-9]+(?=[<>]) by hand: a fresh hook compiles no regex for it (T-3692)
            while j < len(text) and text[j] in "0123456789":
                j += 1
            if text[j:j + 1] in ("<", ">"):
                out.append(" ")  # review: a space, never nothing: ;2>f must stay ; and >f, not the one token ;>
                i = j
                continue
        if ch == _ESC:
            out.append(text[i:i + 2] if encoded else _ESC + _ESC)
            i += 2 if encoded else 1
            continue
        if ch == "\\" and q != "'":
            nxt = text[i + 1:i + 2]
            out.append(ch + (hide(nxt) if nxt != _ESC else ""))
            tail = "" if nxt else "\\"
            i += 2 if nxt and nxt != _ESC else 1
            continue
        if q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        out.append(hide(ch) if q else ch)
        i += 1
    return "".join(out), tail + (q or "")


def _shlex_words(text, close=False, encoded=False):
    """shlex's words, quoted operators as _Quoted; close: an odd quote runs to the end of the text, a trailing \\ is
    itself."""
    hidden, closer = _hide_quoted_ops(text, encoded)
    lex = shlex.shlex(hidden + closer if close else hidden, posix=True, punctuation_chars=_OPS)
    lex.whitespace_split = True
    lex.commenters = ""
    out = []
    for t in lex:
        w = _decode(t) if _ESC in t else t
        out.append(_Quoted(w) if w is not t and _op_word(w) else w)
    return out


def _tokens(cmd):
    """T-1778: by text, as one command is tokenized by many checks (a fresh list each call: callers may change it)."""
    return list(_tokens_of(cmd))


@functools.lru_cache(maxsize=256)
def _tokens_of(cmd):
    return tuple(_tokens_read(cmd))


def _tokens_read(cmd):
    text = _strip_comments(cmd)
    try:
        return _shlex_words(text)
    except ValueError:
        pass
    # unbalanced to shlex (a quote inside "$( )" starts a fresh context to bash, or a later line's odd quote, after bash
    # has run every complete line before it): two readings, each command of both checked (T-1561). First the old split,
    # each word whose own quotes balance read as bash does ('cat' is cat), an odd one kept (the debris rule, T-0982):
    # it never joins words across a space, so nothing hides inside a string a quote scan misplaced. Then shlex again,
    # each " inside a $( ) or `…` within "…" masked and an odd quote run to the end, so X='a b' cat stays cat's prefix
    words = [_bash_word(w) for piece in cmd.split() for w in _pieces(piece)]
    try:
        nested = _shlex_words(_mask_nested_dq(text), close=True, encoded=True)
    except ValueError:
        return words
    d = sum(t.count("(") - t.count(")") for t in words if not isinstance(t, _Quoted) and _op_word(t))
    return words + [";"] + ([")" * d] if d > 0 else ["(" * -d] if d < 0 else []) + [";"] + nested  # depth 0 again


_FELL_BACK = []  # T-3684: set when a system finding was read from text shlex couldn't parse (cleared per Bash check)


def _shlex_fails(cmd):
    """T-3684: _tokens_read took its quote-blind fallback for this line."""
    try:
        _shlex_words(_strip_comments(cmd))
    except ValueError:
        return True
    return False


def _mask_nested_dq(text):
    """Each " inside a $( … ) or `…` that sits within "…" encoded (and each _ESC): bash reads that text in a fresh
    quoting context, while shlex ends the outer string there. Scanned as _inner_quote does; a misread only adds a
    reading."""
    st, esc, out, i = [["", None, 0]], False, [], 0  # [kind, quote, paren depth]
    dq = 0  # the frames under the top one inside "…" (only the top frame's quote changes): no rescan per quote
    while i < len(text):
        ch, top = text[i], st[-1]
        nested = ch == '"' and dq
        out.append(_ESC + "7" if nested else _ESC + _ESC if ch == _ESC else ch)
        if esc:
            esc = False
        elif ch == "\\" and top[1] != "'":
            esc = True
        elif top[1] == "'":
            top[1] = None if ch == "'" else "'"
        elif ch == "`":
            if top[0] == "`" and top[1] is None:
                st.pop()
                dq -= st[-1][1] == '"'
            else:
                dq += top[1] == '"'
                st.append(["`", None, 0])
        elif ch == "$" and text[i + 1:i + 2] == "(":
            dq += top[1] == '"'
            st.append(["$(", None, 0])
            out.append("(")
            i += 2
            continue
        elif top[1] == '"':
            top[1] = None if ch == '"' else '"'
        elif ch in "'\"":
            top[1] = ch
        elif ch == "(" and top[0] == "$(":
            top[2] += 1
        elif ch == ")" and top[0] == "$(":
            if top[2]:
                top[2] -= 1
            else:
                st.pop()
                dq -= st[-1][1] == '"'
        i += 1
    return "".join(out)


def _pieces(piece):
    """One space-split piece of an unparseable line as the words the first reading takes from it: split at ; & | ( ),
    and a redirect from its target too (>~/.bashrc is no plain word: re-review). In a piece holding a quote only a
    leading redirect and one right after a quote, ) or ` ("$(…)">~/.bashrc: re-review): a < or > inside its quoted text is
    text as often as not (awk '$1>4000', the debris rule, T-0982)."""
    if not re.search(r"['\"\\]", piece):
        return re.sub(r"(&?[<>]+[&|(]?|[;&|()])", r" \1 ", piece).split()
    piece = re.sub(r"(?<=[\"')`])(\d*)(&?[<>]+)", r" \1 \2 ", re.sub(r"^(&?[<>]+)(?=.)", r"\1 ", piece))
    return re.sub(r"([;&|()])", r" \1 ", piece).split()


def _bash_word(word):
    if not any(q in word for q in "'\"\\"):
        return word
    try:
        parts = shlex.split(word)
    except ValueError:
        return word
    if len(parts) != 1:
        return word
    return _Quoted(parts[0]) if _op_word(parts[0]) else parts[0]


def _split(tokens):
    cmds, cur, depth = [], Cmd([], [], False), 0
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if isinstance(t, _Quoted):  # '>' or \; is data
            cur.argv.append(t)
            i += 1
            continue
        if _op_word(t):  # every punctuation run, even one read below as a word (<(( …); T-3692: string tests below
            # too, not re.fullmatch per token
            depth += t.count("(") - t.count(")")
        if t in ("<(", ">("):
            cur.procsub = True
            cmds.append(cur)
            cur = Cmd([], [], False, depth=depth)
            i += 1
            continue
        if t and not t.strip(";&|()"):  # [;&|()]+ (an &> run has a >, so it never got here)
            empty = not (cur.argv or cur.redirs or cur.procsub or cur.stdin)  # T-1224: $(< file) reads file
            if t == ";" and empty and cur.op in ("|", "|&", "&&", "||"):
                i += 1  # a newline after | && || continues the command (_lines made it a ;): T-0355 review
                continue
            if not empty:
                cmds.append(cur)
            keep = empty and cur.piped and "(" in t  # T-0581: `| (bash)` and `|⏎(bash)`: the group reads the pipe
            cur = Cmd([], [], t in ("|", "|&") or keep, op=cur.op + t if keep else t, depth=depth)
            i += 1
            continue
        if (u := t[:-1] if t.endswith("|") else t) and not u.strip("<>&"):  # [<>&]+\|?
            t = u  # >| writes even under noclobber (T-0358)
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if t == "<" and nxt == "(":
                cur.procsub = True
                cmds.append(cur)
                depth += 1
                cur = Cmd([], [], False, depth=depth)
                i += 2
                continue
            if t.endswith("&") and (nxt == "-" or nxt.isdecimal()):  # \d+|- (\d is Unicode Nd, as isdecimal)
                i += 2
                continue
            if t in ("<<", "<<<"):
                cur.feeds.append((t, nxt))
            elif t in ("<", "<>"):
                cur.stdin = nxt
            if nxt and (t.startswith(">") or t in ("&>", "&>>", "<>")):
                cur.redirs.append(nxt)
            i += 2
            continue
        cur.argv.append(t)
        i += 1
    if cur.argv or cur.redirs or cur.procsub or cur.stdin:
        cmds.append(cur)
    return cmds


_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "{", "!", "coproc"}  # words before a command
# T-4859: the one table of exec wrappers, {name: (kind, options that take a value)}; _strip_wrappers (every rule) and the
# installer scan (_runs_named, through _skips_to_runner) both read it, so a wrapper is added in this one place.
#   strip   _strip_wrappers drops it and its options (each one's own valued ones: T-3600, exec -c, command -p, setsid -c
#           are flags), the command is the next word: setsid, busybox: T-0160 (the differential test); unbuffer: T-1722
#   strip1  the same, then one positional first: flock's lock file (T-0160), timeout's duration
#   own     stripped by its own branch in _strip_wrappers (env, xargs, rustup run), never a runner word to the scan
#   runner  not stripped (ssh h rm runs on h, a container's paths aren't this machine's); only the installer scan skips
#           past it to the word that runs: ssh h bash, firejail bash …
_SUDO_VALUED = ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U")
_WRAPPER_SPEC = {
    "sudo": ("strip", _SUDO_VALUED), "doas": ("strip", _SUDO_VALUED),
    "nohup": ("strip", ()), "command": ("strip", ()), "builtin": ("strip", ()), "noglob": ("strip", ()),
    "chronic": ("strip", ()), "setsid": ("strip", ()), "busybox": ("strip", ()), "unbuffer": ("strip", ()),
    "npx": ("strip", ()), "bunx": ("strip", ()),
    "time": ("strip", ("-f", "-o", "--format", "--output")),  # GNU time; bash's own takes -p only
    "exec": ("strip", ("-a",)),
    "stdbuf": ("strip", ("-i", "-o", "-e", "--input", "--output", "--error")),
    "nice": ("strip", ("-n", "--adjustment")),
    "ionice": ("strip", ("-c", "-n", "-p", "--class", "--classdata", "--pid")),
    "flock": ("strip1", ("-w", "--timeout", "-E", "--conflict-exit-code")),
    "timeout": ("strip1", ("-s", "-k", "--signal", "--kill-after")),
    "env": ("own", ()), "xargs": ("own", ()), "rustup": ("own", ()),
    "ssh": ("runner", ()), "watch": ("runner", ()), "parallel": ("runner", ()), "script": ("runner", ()),
    "systemd-run": ("runner", ()), "su": ("runner", ()), "chroot": ("runner", ()), "nsenter": ("runner", ()),
    "unshare": ("runner", ()), "firejail": ("runner", ()), "bwrap": ("runner", ()), "docker": ("runner", ()),
    "podman": ("runner", ()), "kubectl": ("runner", ()), "tmux": ("runner", ()), "screen": ("runner", ()),
    "at": ("runner", ()), "batch": ("runner", ()), "runuser": ("runner", ()),
}


def _skips_to_runner(name):
    """Whether the installer scan reads the words after this command name as what runs (ssh h bash, setsid bash …):
    every name in _WRAPPER_SPEC but the 'own' ones, which _strip_wrappers removes (or, rustup alone, doesn't) already."""
    return _WRAPPER_SPEC.get(name, ("own",))[0] != "own"


_CDS = ("cd", "pushd", "popd")  # T-1923: the builtins that move the shell


def _strip_wrappers(argv, dropped=None):
    """Drop env assignments and exec wrappers (sudo, env, timeout, xargs, npx…). Returns (argv, via_xargs). A list given
    as `dropped` gets the words dropped, as read (an env -S string split into its words: T-4686)."""
    i = 0
    while i < len(argv):
        a = argv[i]
        base = os.path.basename(a)
        n = a.find("=")  # NAME= or NAME+= ([A-Za-z_][A-Za-z0-9_]*\+?=) by string tests, no regex to compile: T-3692
        name = a[:n - 1] if n > 0 and a[n - 1] == "+" else a[:n]
        if n > 0 and name.isascii() and name.isidentifier() or a in _KEYWORDS:  # T-0159: `do rm …` is rm; T-0270: +=
            i += 1
        elif a == "function" and i + 1 < len(argv):  # round 11, 12: `function f { CMD; }`, `function f if CMD; then …` run
            i += 2 + (argv[i + 2:i + 3] == ["()"])  # CMD, as `f() { CMD; }` does (the body's keyword is dropped next)
        elif _WRAPPER_SPEC.get(base, ("",))[0] in ("strip", "strip1"):  # T-4859: _WRAPPER_SPEC's names and valued options
            kind, valued = _WRAPPER_SPEC[base]
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] in valued else 1
            i += kind == "strip1"  # flock's lock file, timeout's duration: the command comes after
        elif base == "env":
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or "=" in argv[i]):
                split = _env_split(argv[i:i + 2])
                if split:  # T-3600: env -S 'sampo serve' runs the string's words, read as typed in its place
                    took, text = split
                    try:
                        words = shlex.split(text)
                    except ValueError:
                        words = text.split()
                    argv = argv[:i] + words + argv[i + took:]
                    continue
                i += 2 if argv[i] in ("-u", "-C", "--unset", "--chdir") else 1
        elif base == "xargs":
            i += 1 + _xargs_opts(argv[i + 1:])[0]
        elif base == "rustup" and argv[i + 1:i + 2] == ["run"]:  # T-3607 review: rustup run [--install] TOOLCHAIN CMD…
            i += 2
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            i += 1
        else:
            break
    if dropped is not None:
        dropped += argv[:i]
    return argv[i:], any(os.path.basename(a) == "xargs" for a in argv[:i])


def _env_split(words):
    """T-3600: (words taken, string) for env's -S/--split-string (-S STR, -iS STR, -SSTR, --split-string[=]STR, a
    getopt abbreviation of it), else None. In a cluster -u and -C take the rest, so an S after them is their value."""
    w, nxt = words[0], words[1] if len(words) > 1 else ""
    if not w.startswith("-"):  # NAME=value
        return None
    if w.startswith("--"):
        flag, eq, v = w.partition("=")
        return ((1, v) if eq else (2, nxt)) if len(flag) > 2 and "--split-string".startswith(flag) else None
    for k, ch in enumerate(w[1:], 1):
        if ch in "uC":
            return None
        if ch == "S":
            return (1, w[k + 1:]) if w[k + 1:] else (2, nxt)
    return None


def _xargs_opts(words):
    """T-1123: (words xargs's options take, whether one changes how stdin splits or is replaced), read as getopt does:
    a cluster's value options (n s P L E I a d, and the old e i l) swallow the rest of the cluster or the next word."""
    i, odd = 0, False
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        w = words[i]
        i += 1
        if w == "--":
            break
        if w.startswith("--"):
            odd = odd or bool(re.match(r"--(?:arg-file|delimiter|replace|null|max-lines|eof)", w))
            i += 1 if w in ("--max-args", "--max-procs", "--max-chars", "--arg-file", "--delimiter", "--eof") else 0
            continue
        odd = odd or any(ch in "adEeIiLl0" for ch in w[1:])  # any such letter, even inside a value: doubt is unknown
        for k, ch in enumerate(w[1:], 1):
            if ch in "nsPLEIad":
                i += 1 if k == len(w) - 1 else 0
                break
    return i, odd


def _positionals(args):
    return [a for a in args if not a.startswith("-")]


def _name(argv):
    stripped, _ = _strip_wrappers(argv)
    return os.path.basename(stripped[0]) if stripped else ""


# ---------------------------------------------------------------- bash checks

_SHELLS = lazy(r"^((ba|z|da|k|fi|c|tc)?sh|python[0-9.]*|perl|ruby|node|php|pwsh)$")
# T-1786: whose -c text is shell, read again as bash (pwsh's rm is close enough). python's -c code is python, read by
# its own reader (_spawned_commands); perl, ruby and node -c is a syntax check, php -c a config file
_C_SHELLS = lazy(r"^((ba|z|da|k|fi|c|tc)?sh|pwsh)$")
_FETCHERS = {"curl", "wget", "fetch"}
_FETCH_WORD = lazy(r"(?<![\w./-])(?:\S*/)?(?:curl|wget|(?<!git )fetch)(?![\w.-])")  # T-0421: a fetcher anywhere; T-0937: not `git fetch`
_PRINTED_SUBST = lazy(r"(\$\(|`)\s*((?:(?:command\s+|builtin\s+)?(?:\S*/)?(?:echo|printf))\s[^`)]*?)\s*(?:\)|`)")
_DOWNLOAD_SUBST = lazy(r"(\$\(|`)\s*(curl|wget|fetch)\b")


_INTERP_NAMES = (r"(?:python|perl|ruby|php|pypy|ipython|lua)[0-9.]*|py|jruby|truffleruby|irb|node|nodejs|deno|bun"
                 r"|luajit|Rscript|julia|tclsh")  # T-1585 review: pypy, ipython, lua, R, julia, tcl
_INTERP = lazy(rf"(?:^|[\s;&|(/`])({_INTERP_NAMES}|R(?=\s+-))(?=\s|$|[;&|)`])")  # T-4686: R -e CODE (a bare R is a word)
_TEXT_TICKS = lazy(r"(?:python[0-9.]*|py|node|nodejs|deno|bun)$")  # a backtick is only text in these
_WRITE_API = lazy(
    r"""open\s*\([^)]*['"][rwxab+]*[wxa+][rwxab+]*['"]|\.write_(?:text|bytes)\s*\(|(?:write|append)FileSync|"""
    r"createWriteStream|\bos\.(?:replace|rename|remove|unlink)\b|\bshutil\.\w+\(|\.(?:unlink|rename|replace|touch)\(|"
    r"File\.write|file_put_contents|open\s*\(\s*(?:my\s+)?\$?\w+\s*,\s*['\"]?[>+]")
# T-4736: writes the _WRITE_API text can't see: open(p, 'r', opener=f) opens with whatever flags f chooses; a writer passed
# on as a value (map(shutil.rmtree, names), pool.map(Path.unlink, paths), map(open, names, modes))
_OPENER = lazy(r"\bopener\b|\b(?:i?map\w*|starmap|submit)\s*\(\s*(?:[\w.]*\.)?(?:shutil\.\w+|Path\.\w+)|"
               r"\bmap\s*\(\s*open\s*,[^,)]*,|\bopen\s*\((?:[^(),]*,){6}\s*(?:True|False|[01])\s*,")  # …, closefd, opener
# T-0344: .replace( stays a write here even with two string arguments: os.replace('a', 'b'), an aliased os or (os) look
# the same as str.replace to a text match, and only a proved script (_open_targets) can tell them apart
_QUOTED = lazy(r"""(['"])((?:[~/.]|[\w.-]+/)[^'"\s]*)\1""")
_GUARDED_BY_PATH = ("core", "state-direct", "credentials", "plugin", "brief")
# A slash command that changes plugins, MCP servers or config, sent to claude as a prompt (argv, stdin or a heredoc).
_SLASH_BODY = (r"/(?:plugins?\s+(?:install|i|enable|disable|uninstall|remove|update|"
               r"marketplace\s+(?:add|remove|rm|update))|"
               r"mcp\s+(?:add|remove|enable|disable)|config\s+(?:set|add|remove))\b")
_SLASH_CHANGE = lazy(r"(?:^|[\s'\"])" + _SLASH_BODY)
# Interpreter code that runs claude's plugin/mcp/config subcommands (argv list, split argv array or command string) or
# sends such a slash command. Both an exec API and the command shape must appear, so code that merely mentions them
# isn't blocked.
_CLAUDE_IN_CODE = lazy(r"""['"]claude['"][^;\n]{0,40}?['"](?:plugins?|mcp|config)['"]|"""
                             r"""['"`]claude\s+(?:plugins?|mcp|config)\b|['"]\s*""" + _SLASH_BODY)
_EXEC_API = lazy(r"\bsubprocess\b|\bos\.(?:system|popen|exec\w*|spawn\w*)\b|\bPopen\b|child_process|"
                       r"\b(?:exec|execSync|spawn|spawnSync|system)\s*\(")


# Any Foreman module (fm*.py in plugin/lib), so new modules are covered without editing this list. Calls into the entry
# point modules count as mutating; fmcore/fmguard/fmdocs/fmdoctor are mostly read-only, so their mutators are by name.
# (T-4906 review: fm[a-z_]+, as fmlane_overlap and fmflake_forecast are twins too)
_FM_INTERNALS = lazy(r"\b(?:import|from)\s+(?:fm[a-z_]+|sampo(?:\.\w+)?)\b|(?:__import__|import_module)\s*\(\s*['\"](?:fm[a-z_]+|sampo(?:\.\w+)?)")
_SAMPO_ROLES = {"guard": "ward", "genesis": "rune", "exam": "louhi", "moonshot": "moons", "code": "smith",
                "codebench": "smithbench", "codemod": "smithmod", "codeterm": "smithterm", "codetui": "smithtui"}


def _sampo_name(fm):
    """T-2745: the sampo module an fm module becomes (T-2742's map): fmcli -> sampo.cli, fmguard -> sampo.ward."""
    name = fm[2:] if fm.startswith("fm") else fm
    return "sampo." + _SAMPO_ROLES.get(name, name)


_FM_ENTRY_MODULES = ("fmcli", "fmhooks", "fmsetup", "fmtidy", "fmideas", "fmserve", "fmplugins")
# each fm entry module and its sampo twin; a bare `sampo` (the package) counts too, to be safe
_FM_ENTRY_NAMED = "(?:" + "|".join(_FM_ENTRY_MODULES + tuple(re.escape(_sampo_name(m)) for m in _FM_ENTRY_MODULES)) + ")"
_FM_ENTRY = "(?:" + _FM_ENTRY_NAMED + r"|sampo(?!\.\w+\b(?!\s*\())" + ")"  # bare: not `sampo.mod…` (a module, judged by its own name) but `sampo.fn(`
_FM_WRITERS = (r"save_brief|write_meta|update_meta|write_atomic|log_event|regen_views|init_project|"
               r"add_rule|drop_rule|add_veto|drop_veto|"  # T-0971 review: the user's rules and vetoes
               r"checkpoint|mutate|cmd_\w+|task_\w+|_resolve_approvals|_activate_fallback|restore_default_state")
_FM_WRITER_NAME = lazy(rf"(?:{_FM_WRITERS})")  # fullmatch against one name the ast read
# T-4893: what in code reaches a writer, read as text: a writer called, an entry module called or imported. A from-import
# is judged by the names the ast reads (_fm_writer_named), not by this text
_WRITES = (rf"\b(?:{_FM_WRITERS})\s*\(|"
           rf"\bgetattr\s*\(\s*{_FM_ENTRY}\b|"
           rf"\b{_FM_ENTRY}\s*\.\s*\w+\s*\(|\bfrom\s+{_FM_ENTRY_NAMED}\s+import\b|\bimport\s+{_FM_ENTRY}\s+as\b|"
           rf"(?:__import__|import_module)\s*\(\s*['\"]{_FM_ENTRY}")
_FM_WRITES = lazy(_WRITES)
# T-4893: a from-import of the bare package is an entry import only when it names an entry module (from sampo import
# cli), main or *; `from sampo import landlock` is a module judged by its own name, as `import sampo.landlock` always was
_FM_ENTRY_LEAVES = frozenset({_sampo_name(m).split(".")[1] for m in _FM_ENTRY_MODULES} | {"main"})
# the text gate's own reads: those writes, or a writer named without a call (f = c.write_meta; f(p, 1))
_FM_MUTATORS = lazy(rf"{_WRITES}|\.\s*(?:{_FM_WRITERS})\b")
_FROM_FM = lazy(r"\bfrom\s+(?:fm[a-z_]+|sampo(?:\.\w+)?)\s+import\b(\s*\*)?")
_FROM_NAMES = lazy(rf"\b(?:{_FM_WRITERS}|{'|'.join(sorted(_FM_ENTRY_LEAVES))})\b")


def _nfkc(text):
    """T-4893: text as python reads its names (NFKC: `ｗrite_meta` is write_meta, `ｓampo` is sampo), for matching
    only: the shell's own reading (quotes, separators) is never taken from it."""
    if text.isascii():
        return text
    import unicodedata  # here, not at the top: only non-ASCII text pays for it
    return unicodedata.normalize("NFKC", text)


def _fm_gate(text, idx=None):
    """T-4893: whether text may reach a Sampo writer, read before any parse (text it can't parse is judged by this
    alone): a writer or entry call, a writer named as an attribute, or a from-import of a Sampo module with * or with a
    writer, an entry module or main anywhere between it and the next from-import (past the statement too: a `)` in a
    comment ends no list here; more is refused, never less). Each window is read once, so it is linear in the text.
    T-4906: with the writer index, also any name the text calls or reads as an attribute that the index knows writes
    (any module's writer, a writer method, a class that writes) or that reaches one by name (getattr, vars …)."""
    text = _nfkc(text)  # python reads a fullwidth letter in a name as the plain one
    if _FM_MUTATORS.search(text):
        return True
    ms = list(_FROM_FM.finditer(text))
    if any(m.group(1) or _FROM_NAMES.search(text, m.end(), ms[i + 1].start() if i + 1 < len(ms) else len(text))
           for i, m in enumerate(ms)):
        return True
    if not idx:
        return False
    named = set(_DOTTED.findall(text)) | set(_CALLED.findall(text))
    return bool(named & _index_writer_names(idx) or named & _BY_NAME)


# ---------------------------------------------------------------- T-4906: Sampo's writers, read from its code
# The hand list above (_FM_WRITERS) missed set_panic, lift_project_pauses, set_standing_autonomy and every writer added
# since. The index reads them from the code instead: per Sampo module its top-level functions (writers apart), classes
# (whether a method writes) and other names; a function writes when a call of its, resolved by module alias, self, a
# class or a method's name, reaches one of the seeds below. A use the index can't show is safe is refused.

_INDEX_VERSION = 2
_INDEX_MEMO = {}  # (root, stamp) -> index: one cache read or build per process
_OS_WRITES = frozenset(("replace", "rename", "renames", "remove", "unlink", "rmdir", "removedirs", "truncate",
                        "ftruncate", "symlink", "link", "makedirs", "mkdir", "kill", "killpg", "chmod", "chown",
                        "lchown", "utime", "write", "open", "mkfifo", "mknod", "system", "popen", "fork", "forkpty",
                        "setsid", "putenv", "unsetenv", "chdir"))
_SHUTIL_READS = frozenset(("which", "get_terminal_size", "disk_usage", "ignore_patterns"))
# (a path's rename and replace are left out: a str's replace has the name; os.rename and os.replace are seeds)
_PATH_WRITES = frozenset(("write_text", "write_bytes", "touch", "symlink_to", "hardlink_to", "unlink", "rmdir", "mkdir",
                          "chmod"))
_SPAWN_MODULES = frozenset(("subprocess", "pty", "multiprocessing", "sqlite3", "socket", "urllib", "http"))
_BY_NAME = frozenset(("getattr", "setattr", "delattr", "vars", "globals", "locals", "eval", "exec", "compile",
                      "__import__", "import_module", "__dict__", "__getattribute__", "attrgetter", "methodcaller"))
_DOTTED = lazy(r"\.\s*([A-Za-z_]\w*)")
_CALLED = lazy(r"([A-Za-z_]\w*)\s*\(")
# a method name this common is no edge by its name alone: a pattern's search, a dict's update, a file's write. The
# index follows a Sampo class's method of such a name only through self, cls or a resolved call (a sampo object's
# .search() reached by name alone is the gap this leaves; by name, every regex call made every function a writer)
_PLAIN_METHODS = frozenset(set(dir(dict)) | set(dir(list)) | set(dir(str)) | set(dir(set)) | set(dir(bytes))
                           | set(dir(tuple)) | set(dir(int)) | set(dir(float)) | set(dir(re.Pattern))
                           | set(dir(re.Match)) | set(dir(collections.deque)) | set(dir(collections.Counter))
                           | {"write", "read", "close", "flush", "readline", "readlines", "seek", "tell", "open", "fileno",
                              "strftime", "isoformat", "timestamp", "astimezone", "total_seconds", "date", "time",
                              "exists", "is_file", "is_dir", "resolve", "glob", "iterdir", "stat", "read_text",
                              "read_bytes", "acquire", "release", "wait", "result", "set", "is_set", "put", "get_nowait",
                              "put_nowait", "communicate", "poll", "terminate", "decode", "encode", "hexdigest", "digest"})


def _fm_target(name):
    """The Sampo module an import names (sampo, sampo.X, or an fm twin read as sampo.Y), else ''."""
    if name == "sampo" or name.startswith("sampo."):
        return name
    return _sampo_name(name) if re.fullmatch(r"fm[a-z_]+", name or "") else ""


def _index_files(root):
    """(module, path) of every Sampo module under `root` (plugin/lib/sampo): sampo.X for X.py, sampo for its
    __init__, and the guard's own module beside it (fmguard.py, read as its sampo name)."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = [("sampo" if n == "__init__.py" else "sampo." + n[:-3], os.path.join(root, n)) for n in names if n.endswith(".py")]
    own = os.path.join(os.path.dirname(root), "fmguard.py")
    if os.path.isfile(own) and _sampo_name("fmguard") not in {m for m, _ in out}:
        out.append((_sampo_name("fmguard"), own))
    return out


def _index_stamp(root, files):
    """What the index of `root` is built from: its files' names, sizes and change times (and the index's version)."""
    import hashlib
    h = hashlib.sha1(f"{_INDEX_VERSION}\0{os.path.realpath(root)}\n".encode())
    for m, path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        h.update(f"{m}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:20]


_BARE_SEEDS = frozenset(("Popen", "system", "rmtree", "copyfile", "copy2", "copytree", "move", "flock", "lockf", "kill"))


def _seed_call(n, ast):
    """Whether one call writes on its own: open for writing, an os/shutil/fcntl write, a path's write, a process,
    a database or a socket."""
    return _seed_func(n.func, n, ast)


def _seed_func(fn, n, ast):
    """_seed_call's reading of the callable `fn`; `n` is the call, or None for a callable named without one
    (f = os.replace, partial(shutil.move, …)), whose arguments, an open's mode among them, are then unknown."""
    if isinstance(fn, ast.Name):
        if fn.id == "open":
            if n is None:
                return True
            mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "mode"), None)
            return mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                             and not set(mode.value) & set("wax+"))
        return fn.id in _BARE_SEEDS
    if not isinstance(fn, ast.Attribute):
        return False
    v = fn.value
    root = v
    while isinstance(root, ast.Attribute):
        root = root.value
    base = root.id if isinstance(root, ast.Name) else ""
    if base == "os" and isinstance(v, ast.Name):
        return fn.attr in _OS_WRITES or fn.attr.startswith(("exec", "spawn", "posix_spawn"))
    if base == "shutil":
        return fn.attr not in _SHUTIL_READS
    if base == "fcntl":
        return fn.attr in ("flock", "lockf")
    if base in _SPAWN_MODULES:
        return True
    if fn.attr == "open" and (n is None or n.args):  # a path's open(mode)
        if n is None:
            return True
        mode = n.args[0]
        return not (isinstance(mode, ast.Constant) and isinstance(mode.value, str) and not set(mode.value) & set("wax+"))
    return fn.attr in _PATH_WRITES and not isinstance(v, ast.Constant)  # (a str's replace is no path's)


def _index_module(m, tree, known, ast):
    """One module's functions (key (module, name or Class.name)) and, per function, its seeds and call edges:
    ('f', module, name) resolved, ('m', name) a method of any Sampo class by its name."""
    alias, members = {}, {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            base = ("sampo" + (f".{n.module}" if n.module else "")) if n.level else _fm_target(n.module or "")
            for a in n.names if base else ():
                full = f"{base}.{a.name}"
                if full in known:
                    alias[a.asname or a.name] = full
                elif a.name != "*":
                    members[a.asname or a.name] = (base, a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if t := _fm_target(a.name):
                    alias[a.asname or a.name.split(".")[0]] = t if a.asname or "." not in a.name else "sampo"
    defs = list(_module_level(tree.body, ast))  # (a def under a module-level if or try is the module's too)
    top = {d.name for d in defs if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef))}
    classes = {d.name for d in defs if isinstance(d, ast.ClassDef)}
    funcs = {}  # key -> [seeded, edges]

    def chain(node):
        """the module a dotted name reaches (sampo.core.write_meta -> sampo.core), its last name, else None"""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name) or node.id not in alias:
            return None
        mod = alias[node.id]
        for p in reversed(parts[1:]):
            if f"{mod}.{p}" not in known:
                return None
            mod = f"{mod}.{p}"
        return mod, parts[0]

    def imported(n):
        """the Sampo modules an import statement runs: the named one, the package above it and a from-import's
        submodules"""
        if isinstance(n, ast.Import):
            heads = [_fm_target(a.name) for a in n.names]
        else:
            base = ("sampo" + (f".{n.module}" if n.module else "")) if n.level else _fm_target(n.module or "")
            heads = [base] + [f"{base}.{a.name}" for a in n.names if base]
        out = set()
        for h in filter(None, heads):
            while h:
                if h in known:
                    out.add(h)
                h = h.rpartition(".")[0]
        return out

    # module-level names and the values they hold (T-4906 review: `_move = os.replace` and `_git =
    # functools.partial(c._git, …)` hold writers): each valued name is a function of its own, its value read as a body
    names, values = set(), {}
    for s in _module_level(tree.body, ast):
        got = {x.id for t in (s.targets if isinstance(s, ast.Assign) else [s.target]
                              if isinstance(s, (ast.AnnAssign, ast.AugAssign)) else [])
               for x in ast.walk(t) if isinstance(x, ast.Name)}
        names |= got
        if getattr(s, "value", None) is not None:
            for nm in got:
                values.setdefault(nm, []).append(s.value)
    local = top | set(values)
    # the writers it takes by name from the standard library (from shutil import move): os.replace's kind, bare
    std_seeds = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and not n.level
                 and (n.module or "").split(".")[0] in ("os", "shutil", "fcntl") + tuple(_SPAWN_MODULES)
                 for a in n.names
                 if _seed_func(ast.Attribute(value=ast.Name(id=n.module.split(".")[0], ctx=ast.Load()), attr=a.name,
                                             ctx=ast.Load()), None, ast)}

    def body(key, nodes, cls, deep=True, value=False):
        # [seeded, edges, seeded without its nested defs, edges without them]. A function's nested defs and lambdas
        # count as its own calls for a caller (it may call the closure it gets back: more is refused, never less);
        # what an import runs is read shallow: a def's body, nested or not, runs only when it is called. A callable
        # named without a call (passed on, stored, partial(…)) counts as called for a caller; in a value (`value`) it
        # is what calling the name runs
        rec = funcs.setdefault(key, [False, set(), False, set()])
        walk = _nest_walk(nodes, ast) if deep else ((x, False) for x in _module_level(nodes, ast, calls=True))
        called = set()

        def reach(fn, call):
            got = set()
            if isinstance(fn, ast.Name):
                if fn.id in local:
                    got.add(("f", m, fn.id))
                elif fn.id in classes:
                    got.add(("f", m, f"{fn.id}.__init__"))
                    if value:  # a module-level instance: any of its methods, by any name, may be called on it
                        got.add(("k", m, fn.id))
                elif fn.id in members:
                    got |= {("f", *members[fn.id]), ("f", members[fn.id][0], f"{members[fn.id][1]}.__init__")}
                    if value:
                        got.add(("k", *members[fn.id]))
            elif isinstance(fn, ast.Attribute):
                if (hit := chain(fn)) is not None:
                    got |= {("f", *hit), ("f", hit[0], f"{hit[1]}.__init__")}
                    if value:
                        got.add(("k", *hit))
                elif isinstance(fn.value, ast.Name) and fn.value.id in ("self", "cls") and cls:
                    got.add(("f", m, f"{cls}.{fn.attr}"))
                if call and deep and fn.attr not in _PLAIN_METHODS:  # any object's: a Sampo class's method by that
                    got.add(("m", fn.attr))  # name may be it (an import's own calls, re.compile and the like: never)
            return got

        for n, nested in walk:
            if isinstance(n, (ast.Import, ast.ImportFrom)):  # an import runs the module's own import
                got = {("f", t, "<import>") for t in imported(n)}
                rec[1] |= got
                if not nested:
                    rec[3] |= got
                continue
            if isinstance(n, ast.Call):
                called.add(id(n.func))
                got, seed, runs = reach(n.func, True), _seed_call(n, ast), True
            elif (deep and isinstance(n, (ast.Name, ast.Attribute)) and isinstance(n.ctx, ast.Load)
                  and id(n) not in called):
                # a plain name is a seed only as open or a writer it from-imports (a variable named move is none)
                seed = n.id == "open" or n.id in std_seeds if isinstance(n, ast.Name) else _seed_func(n, None, ast)
                got, runs = reach(n, False), value
            else:
                continue
            rec[0] |= seed
            rec[1] |= got
            if not nested and runs:
                rec[2] |= seed
                rec[3] |= got

    def walk(nodes, cls):
        for d in nodes:
            if isinstance(d, ast.ClassDef):
                walk(list(_module_level(d.body, ast)), d.name)
            elif isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body((m, f"{cls}.{d.name}" if cls else d.name), d.body, cls)
    walk(defs, None)
    # what runs when the module is imported: its statements, class bodies, decorators and defaults, never a def's body
    body((m, "<import>"), tree.body, None, deep=False)
    for nm, vals in values.items():  # what a name holds: calling it, or a method of an instance of it, runs this
        body((m, nm), vals, None, value=True)
    imports = dict(alias) | {k: f"{b}.{n}" for k, (b, n) in members.items()}
    return funcs, top, classes, names, imports


def _nest_walk(nodes, ast):
    """(node, nested) for every node under `nodes`: nested once inside a def's or lambda's body below them (a nested
    def's decorators and defaults run where it is defined, so they are not)"""
    todo = [(n, False) for n in nodes]
    while todo:
        n, nested = todo.pop()
        yield n, nested
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            ran = n.args.defaults + [x for x in n.args.kw_defaults if x is not None] + getattr(n, "decorator_list", [])
            todo += [(c, nested) for c in ran]
            todo += [(c, True) for c in (n.body if isinstance(n.body, list) else [n.body])]
        else:
            todo += [(c, nested) for c in ast.iter_child_nodes(n)]


def _module_level(nodes, ast, calls=False):
    """The statements of a module or class body, those under its if, try, with and loops included, never a def's or
    lambda's body (and never a class's, but as its own statements). calls=True: every node an import runs, the
    expressions inside those statements (a decorator, a default, a class's bases and body) too."""
    todo = list(nodes)
    while todo:
        n = todo.pop()
        fn = isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        if not calls and isinstance(n, ast.stmt) and not isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While,
                                                                        getattr(ast, "TryStar", ast.Try))):
            yield n
            continue
        if calls:
            yield n
        if isinstance(n, ast.Lambda):
            continue
        if fn:  # what runs at the def: its decorators and defaults, not its body
            todo += n.decorator_list + [x for x in n.args.defaults + n.args.kw_defaults if x is not None]
            continue
        todo += [c for c in ast.iter_child_nodes(n) if calls or isinstance(c, (ast.stmt, ast.excepthandler))]


def _build_index(root):
    """T-4906: the writer index of the Sampo modules under `root`: {"version", "modules": {module: {"writers",
    "safe", "classes": {name: writes?}, "names", "import_writes"}}, "methods": writer method names}. A module that
    doesn't parse has no entry, so every name of it is unknown (refused)."""
    import ast
    files = _index_files(root)
    known = {m for m, _ in files}
    funcs, info = {}, {}
    for m, path in files:
        try:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError):
            continue
        fs, top, classes, names, imports = _index_module(m, tree, known, ast)
        funcs.update(fs)
        info[m] = (top, classes, names, imports)
    by_method, by_class = {}, {}
    for (m, q) in funcs:
        if "." in q:
            by_method.setdefault(q.split(".", 1)[1], []).append((m, q))
            by_class.setdefault((m, q.split(".", 1)[0]), []).append((m, q))

    def closure(seeded_at, edges_at):
        """writer -> the writer it calls (None: its own write): every caller of a writer writes, one pass over the
        reverse edges ('f' a function, 'm' a method by its name, 'k' every method of a class)"""
        callers = {}
        for key, rec in funcs.items():
            for e in rec[edges_at]:
                for t in ([(e[1], e[2])] if e[0] == "f" else by_method.get(e[1], ()) if e[0] == "m"
                          else by_class.get((e[1], e[2]), ())):
                    callers.setdefault(t, []).append(key)
        out = {k: None for k, rec in funcs.items() if rec[seeded_at]}
        todo = list(out)
        while todo:
            t = todo.pop()
            for c in callers.get(t, ()):
                if c not in out:
                    out[c] = t
                    todo.append(c)
        return out
    writes = closure(0, 1)
    ran = closure(2, 3)  # what a call runs itself, its nested defs left for whoever calls them: an import's measure
    mods, wcls = {}, {(m, q.split(".", 1)[0]) for (m, q) in writes if "." in q}
    for m, (top, classes, names, imports) in info.items():
        held = {n for n in names - top - classes if (m, n) in writes}  # a name whose value reaches a writer
        mods[m] = {"writers": sorted(n for n in top | held if (m, n) in writes),
                   "safe": sorted(n for n in top if (m, n) not in writes),
                   "classes": {c: (m, c) in wcls for c in sorted(classes)},
                   "names": sorted(names - top - classes - held), "import_writes": (m, "<import>") in ran,
                   # a name it imports from another Sampo module: that module, or its member as module.name
                   "imports": {k: v for k, v in sorted(imports.items()) if k not in top and k not in classes}}
        # what each writer calls that writes, for the refusal's words (a module-qualified name; '' its own write)
        mods[m]["via"] = {n: ".".join(writes[(m, n)]) if writes[(m, n)] else "" for n in mods[m]["writers"]}
    methods = sorted({q.split(".", 1)[1] for (m, q) in writes if "." in q})
    return {"version": _INDEX_VERSION, "modules": mods, "methods": methods}


def _writer_index(root=None, cache=None):
    """T-4906: the writer index of the Sampo tree at `root` (default: the one beside this file), built once per tree
    (its stamp: sizes and change times) and kept in `cache` (Sampo's state folder, which only Sampo writes: a hand-made
    index there would be a way round the Ward), or in this process alone when no cache is given."""
    root = root or os.path.join(os.path.dirname(os.path.abspath(__file__)), "sampo")
    files = _index_files(root)
    stamp = _index_stamp(root, files)
    if (root, stamp) in _INDEX_MEMO:
        return _INDEX_MEMO[(root, stamp)]
    path = os.path.join(cache, f"ward-index-{stamp}.json") if cache else None
    idx = None
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                idx = json.load(f)
            idx = idx if isinstance(idx, dict) and idx.get("version") == _INDEX_VERSION else None
        except (OSError, ValueError):
            idx = None
    if idx is None:
        idx = _build_index(root)
        if path:
            try:
                os.makedirs(cache, exist_ok=True)
                old = sorted((os.path.getmtime(os.path.join(cache, n)), n) for n in os.listdir(cache)
                             if n.startswith("ward-index-"))
                for _, n in old[:-16]:  # the newest trees' indexes stay (lanes keep their own)
                    os.remove(os.path.join(cache, n))
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(idx, f)
                os.replace(tmp, path)
            except OSError:
                pass
    _INDEX_MEMO[(root, stamp)] = idx
    return idx


def _index_writer_names(idx):
    """Every name the index knows writes: a module's writer, a writer method, a class that writes (read once)."""
    got = idx.get("_all")
    if got is None:
        got = idx["_all"] = frozenset({n for i in idx["modules"].values() for n in i["writers"]}
                                      | {c for i in idx["modules"].values() for c, w in i["classes"].items() if w}
                                      | set(idx["methods"]))
    return got


def _index_methods(idx):
    got = idx.get("_methods")
    if got is None:
        got = idx["_methods"] = frozenset(idx["methods"])
    return got


def _member_unsafe(mods, m, name, depth=0):
    """Why `name` of Sampo module `m` may write ('' when the index shows it can't): a writer, a class that writes, or
    a name the index doesn't know (another tree may define it, and a dunder walks the module). A name `m` imports from
    another Sampo module is judged there."""
    info = mods.get(m)
    if info is None:
        return f"{m}.{name} (a module the writer index doesn't know)"
    if (t := info.get("imports", {}).get(name)) and depth < 8:
        if t in mods:  # a module it imports: the caller follows it as a module (_sub_module); here, not proven safe
            return f"{m}.{name} (a Sampo module, by another name)"
        base, _, leaf = t.rpartition(".")
        return _member_unsafe(mods, base, leaf, depth + 1)
    if name in info["writers"]:
        return f"{m}.{name} (it writes)"
    if name in info["safe"] or name in info["names"]:
        return ""
    if name in info["classes"]:
        return f"{m}.{name} (a class that writes)" if info["classes"][name] else ""
    return f"{m}.{name} (a name the writer index doesn't know)"


def _code_writes(tree):
    """The first call in parsed code that writes files or starts a process on its own (as _seed_call reads it)."""
    import ast
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _seed_call(n, ast):
            f = n.func
            return f"{f.id if isinstance(f, ast.Name) else f.attr}()"
    return ""


def _sub_module(mods, m, name):
    """The Sampo module `m.name` is (a submodule, or a module `m` imports under that name), else ''."""
    if f"{m}.{name}" in mods:
        return f"{m}.{name}"
    t = mods.get(m, {}).get("imports", {}).get(name, "")
    return t if t in mods else ""


def _literal_getattr(call, obj, ast):
    """Whether `call` is getattr(obj, "name"[, default]) or hasattr(obj, "name"): an attribute read by its literal name."""
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in ("getattr", "hasattr")
            and len(call.args) in (2, 3) and call.args[0] is obj and not call.keywords
            and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str))


# T-4906: a dunder that names no object walk (the rest — __dict__, __globals__, __class__… — reach any module's names)
_PLAIN_DUNDERS = frozenset(("__name__", "__main__", "__file__", "__doc__"))


def _dynamic_use(tree):
    """T-4906: the first name in parsed code that may run a name the writer index never reads (exec, eval, a computed
    getattr, __import__, a dunder walk, a process that runs code), '' when none. What the code defines itself (def,
    class) is its own, and getattr or hasattr with a literal name is an attribute (_fm_use_refused judges it)."""
    import ast
    own = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    parents = None
    for n in ast.walk(tree):
        name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
        if not name or name in own or name in _PLAIN_DUNDERS or not (name in _DYNAMIC or name.startswith("__")):
            continue
        if name in ("getattr", "hasattr") and isinstance(n, ast.Name):
            if parents is None:
                parents = {ch: p for p in ast.walk(tree) for ch in ast.iter_child_nodes(p)}
            call = parents.get(n)
            if isinstance(call, ast.Call) and call.func is n and call.args and _literal_getattr(call, call.args[0], ast):
                continue
        return name
    return ""


def _fm_use_refused(tree, idx):
    """T-4906: the first use in parsed code of a Sampo module the writer index can't show is safe, as words for the
    refusal, else '': a module whose import writes, a writer or an unknown name reached through any import form
    (alias, from-import, a dotted chain, an fm twin), a module passed on as a value (getattr, vars, f = c), a writer
    method's name on any object, or a file write or process beside the import."""
    import ast
    mods = idx["modules"]
    bound = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if t := _fm_target(a.name):
                    if mods.get(t, {}).get("import_writes"):
                        return f"{t} (its import writes)"
                    bound[a.asname or a.name.split(".")[0]] = t if a.asname or "." not in a.name else "sampo"
        elif isinstance(n, ast.ImportFrom) and not n.level and (base := _fm_target(n.module or "")):
            if mods.get(base, {}).get("import_writes"):  # (T-4906 review: every import form, not only `import`)
                return f"{base} (its import writes)"
            for a in n.names:
                if a.name == "*":
                    return f"{base}.* (every name of it)"
                if sub := _sub_module(mods, base, a.name):
                    if mods.get(sub, {}).get("import_writes"):
                        return f"{sub} (its import writes)"
                    bound[a.asname or a.name] = sub
                elif why := _member_unsafe(mods, base, a.name):
                    return why
    if not bound and not any(isinstance(n, ast.ImportFrom) and _fm_target(n.module or "") for n in ast.walk(tree)):
        return ""
    parents = {ch: p for p in ast.walk(tree) for ch in ast.iter_child_nodes(p)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id in bound and isinstance(n.ctx, ast.Load):
            m, node = bound[n.id], n
            while True:
                par = parents.get(node)
                if isinstance(par, ast.Attribute) and par.value is node:
                    attr = par.attr
                elif _literal_getattr(par, node, ast):  # getattr(c, "fit") is c.fit; hasattr(c, "x") reads nothing
                    if par.func.id == "hasattr":
                        break
                    attr = par.args[1].value
                else:
                    return f"Sampo's module {m} passed on as a value"
                if sub := _sub_module(mods, m, attr):
                    m, node = sub, par
                    continue
                if why := _member_unsafe(mods, m, attr):
                    return why
                break
        elif isinstance(n, ast.Attribute) and n.attr in _index_methods(idx) and n.attr not in _PLAIN_METHODS:
            return f".{n.attr} (a Sampo class's method of that name writes)"
    if w := _code_writes(tree):
        return f"{w} beside a Sampo import (a path it computes may be Sampo's state)"
    return ""


def _fm_writer_named(tree):
    """T-4893: the first name in parsed code that reaches a Sampo writer without a call the text shows: a writer, an
    entry module or * in a from-import of a Sampo module (aliases and (…) lists across lines included), or a writer
    read as an attribute (f = c.write_meta). '' when none. Read from the ast, so no list is scanned by a regex."""
    import ast
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith(FM_MODULE_PREFIXES):
            if re.fullmatch(_FM_ENTRY_NAMED, n.module):
                return n.module
            for a in n.names:
                if a.name == "*" or _FM_WRITER_NAME.fullmatch(a.name) or (n.module == "sampo" and a.name in _FM_ENTRY_LEAVES):
                    return a.name
        elif isinstance(n, ast.Attribute) and _FM_WRITER_NAME.fullmatch(n.attr):
            return n.attr
    return ""


_FM_RUN_ARG = lazy(r"""--run(?:=|\s+)(?:"(?:\\.|[^"\\])*"|'[^']*')""")


def _unescape_ticks(body):
    """A backtick body as the shell runs it: \\`, \\$ and \\\\ lose their backslash (nested backticks: T-0158)."""
    return re.sub(r"\\([`$\\])", r"\1", body)


def _subst_bodies(text, tails=True, clean=False):
    """Every command-substitution body a shell would run in text (T-0158): `…` and $( … ), nested ones too, bare or
    inside double quotes, never inside single quotes or after a backslash; $(( … )) is arithmetic. Each $( opens a fresh
    quoting context, as in bash. With $'…' quoting (where \\' is a quote) the scan can't follow: every span between
    backticks and every greedy $( … ) is returned instead (fails closed). An unterminated body runs to the end."""
    if "$'" in text:
        return [_unescape_ticks(b) for b in text.split("`")[1::2]] + [text[m.end():] for m in re.finditer(r"\$\(", text)]
    ends = []  # every substitution is also read to the end: a case pattern's ) or a comment can close it early
    bodies, ctx, tick, esc, i = [], [[None, 0, 0]], None, False, 0  # ctx: [quote, body start, paren depth]
    while i < len(text):
        ch, top = text[i], ctx[-1]
        if esc:
            esc = False
        elif ch == "\\" and top[0] != "'":
            esc = True
        elif top[0] == "'":
            top[0] = None if ch == "'" else "'"
        elif tick is not None:
            if ch == "`":
                bodies.append(_unescape_ticks(text[tick:i]))
                tick = None
        elif ch == "`":
            tick = i + 1
        elif ch == "$" and text[i + 1:i + 2] == "(":
            ends.append(text[i + 2:])  # $(( … )) too: bash falls back to a subshell when )) doesn't close it
            tail = [len(ends) - 1]  # T-1924: which tails a clean close may drop
            last = text.rfind(")")  # T-0669: and, when it ends inside a quote, up to the last ): an outer `)"`
            if last > i + 2 and _quote_at(text[i + 2:]) is not None:  # left it open (whole code stays whole)
                ends.append(text[i + 2:last])
                tail.append(len(ends) - 1)  # T-4811: a clean body drops this one too: it is the same body and the rest
            if text[i + 2:i + 3] != "(":
                ctx.append([None, i + 2, 0, tail])
            i += 2
            continue
        elif ch == '"':
            top[0] = None if top[0] == '"' else '"'
        elif top[0] is None and ch == "'":
            top[0] = "'"
        elif top[0] is None and ch == "(" and len(ctx) > 1:
            top[2] += 1
        elif top[0] is None and ch == ")" and len(ctx) > 1:
            if top[2]:
                top[2] -= 1
            else:
                bodies.append(text[top[1]:i])
                if clean and not re.search(r"[#`\\]|<<|\(\(|\bcase\b", bodies[-1]):  # T-1924: nothing in it can end
                    for k in top[3]:  # it early, so its tail is its body, already read, then the rest, read by the
                        ends[k] = None
                ctx.pop()
        i += 1
    ends = [e for e in ends if e is not None]  # caller (a tail per substitution from every folder doubled the walk)
    return bodies + ([_unescape_ticks(text[tick:])] if tick is not None else []) + (ends if tails else [])


def _real_fm(argv, ctx):
    """argv runs Sampo's own fm: bare `fm` (the caller has ruled out PATH, alias and function changes), or its real
    path, directly or as python's script. Anything else named fm is just a program (automated review of T-0153)."""
    fm_bins = _FS_MEMO.get(("fm", ctx.foreman_home))  # T-4758: asked once a reading, not once a command (each ask
    if fm_bins is None:  # walks the path with an lstat per folder: the biggest cost of a long script's read)
        fm_bins = _FS_MEMO["fm", ctx.foreman_home] = {
            os.path.realpath(os.path.join(ctx.foreman_home, "plugin", "bin", p)) for p in FM_PROGRAMS}

    def same(path):
        joined = os.path.join(ctx.cwd, os.path.expanduser(path))
        real = _FS_MEMO.get(("real", joined))
        if real is None:
            real = _FS_MEMO["real", joined] = os.path.realpath(joined)
        return real in fm_bins
    if not argv:
        return False
    if argv[0] in FM_PROGRAMS:
        return True
    if re.match(r"^python[0-9.]*$", os.path.basename(argv[0])):
        return len(argv) > 1 and not argv[1].startswith("-") and same(argv[1])
    return same(argv[0])


def _mask_fm(cmd, ctx):
    """The command with each top-level fm command blanked to `fm`: its arguments are data to fm (its --run values are
    checked on their own, substitutions inside are read recursively), so an interpreter named in an fm task's text isn't
    interpreter code (T-0153). Heredoc bodies are kept; where the quote scan can't follow, nothing is blanked."""
    key = ("mask", cmd, ctx.foreman_home, ctx.cwd)  # T-4758: five readers of one command ask for the same text
    got = _FS_MEMO.get(key)
    if got is None:
        got = _FS_MEMO[key] = _mask_fm_read(cmd, ctx)
    return got


def _mask_fm_read(cmd, ctx):
    shell, bodies = _heredocs(cmd)
    if ("$'" in shell or len(re.findall(r"(?<!<)<<(?!<)", shell)) != len(re.findall(_HEREDOC_START, shell))
            or re.search(r"\bPATH\b|\balias\b|\bhash\b|\bfunction\s+" + FM_NAME_RE + r"\b|\b" + FM_NAME_RE + r"\s*\(\s*\)", shell)):
        return cmd  # the quote scan can't follow, or which program `fm` names could change: blank nothing
    # T-4921: a # may start a comment, whose quotes and parentheses the scan would misread: so one outside every quote and
    # escape stops it (a comment, ${#x}, $#, a#b), and so does one in a double quote when a substitution is in the text
    # (a comment inside $( ) or backticks ends at a newline, not at the quote). Inside quotes anywhere else it is prose.
    subst = "$(" in shell or "`" in shell
    out, seg, q, esc = [], [], None, False

    def flush():
        text = "".join(seg)
        argv, _ = _strip_wrappers(_tokens(text))
        fm = _real_fm(argv, ctx)
        # a substitution in fm's arguments runs: keep that segment whole (the $( ) reader misses nested parentheses)
        out.append(" fm " if fm and "$(" not in text and "`" not in text else text)
        seg.clear()
    for ch in shell:
        if esc:
            esc = False
        elif ch == "\\" and q != "'":
            esc = True
        elif ch == "#" and (q is None or (q == '"' and subst)):
            return cmd
        elif q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif ch in ";&|\n()`":
            flush()
            out.append(ch)
            continue
        seg.append(ch)
    flush()
    return cmd if q else "".join(out) + "\n" + bodies  # an unclosed quote: the scan lost track, blank nothing


def _top_level(prefix):
    """True when shell text ending here is outside every quote and escape (a quote scan: wrong only towards False)."""
    return _quote_at(prefix) is None


def _quote_at(prefix):
    """The quote open where shell text ends (None, ' or "); "\\" when it ends inside an escape."""
    q, esc = None, False
    for ch in prefix:
        if esc:
            esc = False
        elif ch == "\\" and q != "'":
            esc = True
        elif q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
    return "\\" if esc else q


_DYNAMIC = {"exec", "eval", "compile", "getattr", "__import__", "import_module", "importlib", "globals", "vars",
            "__builtins__", "builtins", "setattr",
            # code in a string can also run in another process (T-0170, own adversary pass)
            "system", "popen", "Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
            "startfile", "posix_spawn", "posix_spawnp", "fork", "forkpty", "spawnl", "spawnle", "spawnlp", "spawnlpe",
            "spawnv", "spawnve", "spawnvp", "spawnvpe", "execl", "execle", "execlp", "execlpe", "execv", "execve",
            "execvp", "execvpe"}
# a parsed script that imports only these can't load or run the code its strings hold (anything else: read as text)
_PLAIN_IMPORTS = {"re", "json", "os", "sys", "pathlib", "textwrap", "difflib", "collections", "itertools", "functools",
                  "string", "datetime", "time", "shutil", "glob", "fnmatch", "math"}


def _python_units(cmd):
    """(the bodies of quoted heredocs fed straight to python, the rest of the command, the lines that feed them, the
    rest's lines before the last of those): a unit only when it's the line's one heredoc, on a `python - <<` line with
    no pipe, `;`, `&`, backtick or `$(` — after a chain of plain `cd DIR &&` (T-0282: a working directory doesn't
    change what the code does). T-1458: lines after the last unit run after python, so they can't move where it ran."""
    lines, rest, units, starters, seen, i, lead = cmd.split("\n"), [], [], [], [], 0, []
    while i < len(lines):
        line = lines[i]
        i += 1
        starts = _heredoc_starts(seen, line)
        seen.append(line)
        # T-0315: plain name=value; or && first (T-1887: any case, but never one python or bash reads, nor one already
        # exported, whose new value python would inherit) and the script's own arguments (sys.argv: they pick what it
        # reads, never what it may write, which _open_targets proves from literals) don't change what the code does
        run = re.match(r"\s*(?P<pre>(?:[A-Za-z_][A-Za-z0-9_]*=[\w./~@+:=,-]*\s*(?:;|&&)\s*)*)"
                       r"(?:cd\s+[\w./~@+:=,-]+\s*&&\s*)*"
                       r"(?P<py>python[0-9.]*\s+-(?:\s+(?:'[^'\n]*'|\"[^\"`\n]*\"|[\w./~@+:=,$-]+))*\s*<<.*)$", line)
        if run and (re.search(r"\bset\s+-\w*a|allexport", cmd) or any(  # set -a exports them all
                _PRE_DENY.match(n) or n in os.environ for n in re.findall(r"([A-Za-z_]\w*)=", run["pre"]))):
            run = None
        # T-2261: `2>&1 | tail -5` after the heredoc only filters what python prints: dropped before the checks below,
        # unless the shell text could make a filter's name run something else (a function, PATH, alias, eval, source).
        # The tail's own words are filter arguments; the rest of the shell text (this line up to the tail, every other
        # line) is checked with heredoc delimiter quotes removed, so any other quote or backslash refuses the widening
        py = run["py"] if run else ""
        if py and (tail := _READ_TAIL.search(py)).group():
            head = line[:len(line) - len(py) + tail.start()]
            shell = _DELIM_QUOTES.sub("", _strip_heredocs(cmd).replace(line, head, 1))
            if not _FILTER_REDEFINED.search(shell):
                py = py[:tail.start()]
        # review (T-0286): bash must read the body the guard parses — no continued line (`\`), no second `<<` form
        # the heredoc scan doesn't know (`<<\E`: the last redirect wins)
        fed = len(starts) == 1 and starts[0][0] and run and not re.search(r"[|`;&\\]|\$\(", py) and \
            len(re.findall(r"(?<!<)<<(?!<)", line)) == 1
        if fed:
            lead = list(rest)
        (starters if fed else rest).append(line)
        for quote, delim in starts:
            body = []
            while i < len(lines) and not _ends_body(lines[i], delim, line):
                body.append(lines[i])
                i += 1
            if i < len(lines):
                rest.append(lines[i])
                i += 1
            (units if fed else rest).append("\n".join(body))
    return units, rest, starters, lead


# T-2261: what may follow a heredoc fed to python on its line: a stderr merge, then pipes into filters that write no
# file and run nothing, each word literal (no $, backtick, glob, redirect). sort loses -o/--output, -T and
# --compress-program (any short cluster holding o or T, any --o/--c/--t abbreviation); uniq takes flags only, never `--`
# or a lone `-` (its second file is an output, and after `--` even -name is one); tail never follows (-f/-F/--follow
# never ends). Bare words only for sort and uniq: a quoted '-o' is still -o.
_TAIL_ARG = r"""(?:[\w.,:=+/@%^-]+|'[^'\n]*'|"[^"$`\\\n!]*")"""
_READ_TAIL = lazy(r"(?:\s+2>&1)?(?:\s*\|\s*(?:(?:head|cut|wc|grep)(?:\s+" + _TAIL_ARG + r")*"
                  r"|tail(?:\s+(?!['\"]?(?:-\w*[fF]|--f))" + _TAIL_ARG + r")*"
                  r"|sort(?:\s+(?!-[^-\s]*[oT]|--[oct])[\w.,:=+/@%^-]+)*|uniq(?:\s+-(?!-)[\w.,:=+/@%^-]+)*))*\s*$")
# shell text that could make a filter's name run something else (the guard reads `tail` as the coreutil); a quote or a
# backslash outside a heredoc's own delimiter refuses too: e\val, ev""al, s\ource and '.' run the builtin all the same
_FILTER_REDEFINED = lazy(r"\b(?:PATH|alias|hash|enable|function|eval|source|trap|shopt|BASH_ENV|BASH_CMDS|BASH_ALIASES)\b"
                         r"|(?:^|[\s;&|(])\.\s|\b(?:head|tail|cut|wc|grep|sort|uniq)\s*\(|[\\'\"]")
_DELIM_QUOTES = lazy(r"""<<-?\s*(['"])\w+\1""")
# T-1887: names python, the loader, bash or Foreman read: never a plain prefix of a heredoc the guard parses
_PRE_DENY = lazy(r"(?:PATH|HOME|IFS|CDPATH|ENV|BASH_ENV|TMPDIR|GCONV_PATH|LOCPATH|NLSPATH|GLIBC_TUNABLES|LD_\w*"
                       r"|MALLOC_\w*|PYTHON\w*|CLAUDE\w*|FOREMAN\w*|SAMPO\w*|FM_\w*|XDG_\w*|CODEX_\w*)$")


# module → (functions it may call, values it may name: flags, exception classes, argv); nothing that holds a module
_PY_PURE = {"re": (set("sub subn search match fullmatch findall finditer split compile escape".split()),
                   set("error I IGNORECASE M MULTILINE S DOTALL X VERBOSE A ASCII".split())),
            "json": ({"loads", "dumps", "load", "dump"}, {"JSONDecodeError"}),
            "textwrap": ({"dedent", "indent", "fill", "wrap", "shorten"}, set()),
            "sys": ({"exit"}, {"argv", "stdin"})}  # T-0344: reading stdin writes nothing
_FRAME_ATTR = lazy(r"(?:gi|cr|ag|f|tb)_")
_PY_PATH_VARS = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONPLATLIBDIR", "PYTHONSTARTUP")  # what imports load
# review: the only builtins a proved script may name (not type: type(f.buffer.raw) is FileIO, which opens files; not
# locals: locals()['__builtins__'].open, or a name rebound behind the binding count)
_PY_BUILTINS = set("""print len str int float bool bytes bytearray list dict set frozenset tuple range enumerate zip
    sorted reversed min max sum any all abs round repr isinstance map filter chr ord hex oct bin format hash iter next
    divmod pow slice input exit Exception ValueError KeyError IndexError TypeError OSError FileNotFoundError
    RuntimeError AssertionError NotImplementedError StopIteration SystemExit UnicodeDecodeError""".split())


def _open_targets(code):
    """T-0176: the paths a python script can write, or None when that can't be proved. Proved when nothing in it can
    reach a module that writes: no import but the pure functions of _PY_PURE (used as module.function only), nothing
    dynamic (_DYNAMIC, breakpoint, help), no dunder or frame attribute, the builtin open never rebound or passed on.
    Builtin open is then its only writer, and every open with a write mode (or one it can't read) must name a literal,
    or a name bound once at the top level to one (+ joins them)."""
    import ast
    import collections
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    nodes = list(ast.walk(tree))
    parent = {c: n for n in nodes for c in ast.iter_child_nodes(n)}
    names = {n.id for n in nodes if isinstance(n, ast.Name)}
    if names & _DYNAMIC or any(x.startswith("__") for x in names) or any(
            isinstance(n, ast.Attribute) and (n.attr in _DYNAMIC or n.attr.startswith("__") or n.attr == "open"
                                              or _FRAME_ATTR.match(n.attr)) for n in nodes) or any(
            isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("__") for n in nodes) or \
            any(isinstance(n, (ast.ImportFrom, ast.Match, ast.Global, ast.Nonlocal)) for n in nodes):
        return None
    mods = {a.name for n in nodes if isinstance(n, ast.Import) for a in n.names}
    if any(a.name not in _PY_PURE or a.asname for n in nodes if isinstance(n, ast.Import) for a in n.names):
        return None
    bound = collections.Counter()
    for n in nodes:
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            bound[n.id] += 1
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[n.name] += 1
        elif isinstance(n, ast.arg):
            bound[n.arg] += 1
        elif isinstance(n, ast.alias):
            bound[n.name] += 1
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound[n.name] += 1
    if bound["open"] or any(bound[m] != 1 for m in mods):
        return None
    if {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} - set(bound) - _PY_BUILTINS \
            - {"open"}:
        return None  # a builtin outside the plain list
    for n in nodes:
        p = parent.get(n)
        if isinstance(n, ast.Name) and n.id in mods:
            calls, values = _PY_PURE[n.id]
            called = isinstance(parent.get(p), ast.Call) and parent[p].func is p
            if not (isinstance(p, ast.Attribute) and p.value is n and (
                    p.attr in values or (p.attr in calls and called))):
                return None  # the module passed on, a part outside the list (re.enum.bltns…), a function not called
        if isinstance(n, ast.Name) and n.id == "open" and not (isinstance(p, ast.Call) and p.func is n):
            return None
    top = {s.targets[0].id: s.value for s in tree.body if isinstance(s, ast.Assign) and len(s.targets) == 1
           and isinstance(s.targets[0], ast.Name) and bound[s.targets[0].id] == 1}

    def text(e, depth=0):
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            return e.value
        if isinstance(e, ast.Name) and e.id in top and depth < 20:
            return text(top[e.id], depth + 1)
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add) and depth < 20:
            a, b = text(e.left, depth + 1), text(e.right, depth + 1)
            return None if a is None or b is None else a + b
        return None

    targets = []
    for n in nodes:
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open":
            if any(k.arg in (None, "opener") for k in n.keywords) or any(isinstance(a, ast.Starred) for a in n.args) \
                    or len(n.args) > 7:  # T-4736: an opener (keyword or the 8th argument) opens with any flags, whatever the mode says
                return None
            kw = {k.arg: k.value for k in n.keywords}
            mode = n.args[1] if len(n.args) > 1 else kw.get("mode")
            m = "r" if mode is None else text(mode)
            if m is None or set(m) & set("wax+"):
                path = text(n.args[0] if n.args else kw.get("file"))
                if path is None:
                    return None
                targets.append(path)
    return targets


def _chain_dir(starters, shell, ctx):
    """T-0774: the one folder every python unit runs in, when each starter changes folder only by `cd DIR &&` links
    (python runs only after each succeeded) to absolute, ~ or ./ ../ targets (no CDPATH lookup, no `cd -`) and nothing
    else in the command changes folder; None when that isn't certain (every named folder counts then). Review: a
    regex for folder changes misses "cd", \\cd, source and eval, so the rest of the command may hold nothing but the
    heredocs' own end lines."""
    ends = {m.group(1) for s in starters for m in [re.search(r"<<-?\s*'(\w+)'", s)] if m}
    if any(line.strip() and line.strip() not in ends for line in shell.splitlines()):
        return None
    dirs = set()
    for s in starters:
        d = ctx.cwd
        for t in re.findall(r"\bcd\s+([\w./~@+:=,-]+)\s*&&", s):
            t = _expand(t, ctx)
            if _unresolvable(t) or not re.match(r"(/|\./|\.\./|\.\.?$)", t):
                return None
            if ".." in t and _walk(t, d)[1]:  # T-4673: a '..' after a link: two folders, not the one
                return None
            d = _resolve(t, d)
        dirs.add(d)
    return dirs.pop() if len(dirs) == 1 else None


def _proved_writes(cmd, ctx):
    """T-0176: the files a command's python can write, as paths, when every interpreter in it is a quoted heredoc fed
    straight to python whose writes _open_targets proves; None otherwise (the coarse rule decides). A relative path
    after a cd counts in every folder the command names too."""
    units, rest, starters, lead = _python_units(cmd)
    shell = "\n".join(rest)
    around = "\n".join(rest + starters)  # review (T-0286): a cd on the python line itself picks the folder too
    if not units or _INTERP.search(_mask_fm(_drop_data_heredocs(shell), ctx)):
        return None
    # review: python's own settings (PYTHONPATH on a continued line, or inherited) choose what an import runs
    if "PYTHON" in shell or re.search(r"\\\s*$", shell, re.M) or any(os.environ.get(k) for k in _PY_PATH_VARS):
        return None
    targets = []
    for code in units:
        found = _open_targets(code)
        if found is None:
            return None
        targets += found
    bases = [ctx.cwd]
    if re.search(r"(?:^|[\s;&|(])(?:cd|pushd|popd)\b", around):
        only = _chain_dir(starters, "\n".join(lead), ctx)
        bases = [only] if only else bases + [
            d for d in (_resolve(_expand(w, ctx), ctx.cwd) for w in _WORD.findall(around) if not _unresolvable(w))
            if os.path.isdir(d)] + _climb_dirs(around, ctx)  # T-4673: and where a climbing cd really goes
    import ast
    mods = {a.name for code in units for n in ast.walk(ast.parse(code)) if isinstance(n, ast.Import) for a in n.names}
    if any(os.path.exists(os.path.join(b, m + x)) for b in bases for m in mods for x in (".py", "")):
        return None  # python - searches the current folder first: a json.py there would be what runs
    return list(dict.fromkeys(_walk(_expand(t, ctx), b) for t in targets for b in bases))  # T-4667: (path, real)


_SOURCE_WRITE = lazy(r"(?:cat\s+>>?\s*|tee\s+(?:-a\s+)?)([\w./~@+-]+)\s*(?:>\s*/dev/null\s*)?<<-?\s*"
                           r"(?:'\w+'|\"\w+\")\s*$")  # a quoted marker: the body is data, never expanded
# T-0981: Foreman's code and tests, in the checkout or in a builder worktree's copy of it
_SOURCE_DIRS = lazy(r"(?:\.claude/worktrees/[\w.-]+/)?plugin/(?:" + "|".join(_CORE_DIRS) + r"|tests)/")


def _writes_unrun_source(line, cmd, ctx):
    """T-0761: a data heredoc writing one file of Sampo's own code (T-0981: or its tests, or a worktree's copy) that
    the command never names again is a source edit (a core path write the user's yes gates), not interpreter code run
    here. Run or imported by name, it is."""
    m = _SOURCE_WRITE.fullmatch(line.strip())
    if not m:
        return False
    path = _resolve(_expand(m.group(1), ctx), ctx.cwd)
    stem = re.escape(os.path.splitext(os.path.basename(path))[0])
    real = os.path.realpath(path)  # a symlink in plugin/lib to a scratch script isn't Foreman's code
    home = os.path.realpath(ctx.foreman_home)
    rel = os.path.relpath(real, home) if _strictly_under(real, home) else ""
    return bool(_SOURCE_DIRS.match(rel)) and len(re.findall(rf"\b{stem}\b", _drop_data_heredocs(cmd))) == 1


_DRAFT_EXT = (".txt", ".patch", ".diff", ".md", ".json")
# review: only flags that take no value (-W/-X would take the script's place, leaving the draft to run)
_DRAFT_USE = lazy(r"\s*" + FM_NAME_RE + r"\s+edit\b.*|\s*python[0-9.]*\s+(?:-[BEIOPRSbdqsuv]+\s+)*([\w./~@+-]+\.py)\s.*")


def _writes_unrun_draft(line, cmd, ctx):
    """T-1917: a data heredoc writing a scratch draft (.txt/.patch…) is text, though it names Sampo's modules, when
    the command names the draft again only as fm edit's input or a script's argument, never as code: no wider than the
    two-command form (write the file, then run). Fails closed on anything that could spell its name another way (globs,
    expansion, quotes, escapes), a cd, a second heredoc, or a script that isn't already a plain file on disk."""
    m = _SOURCE_WRITE.fullmatch(line.strip())
    if not m:
        return False
    path = os.path.realpath(_resolve(_expand(m.group(1), ctx), ctx.cwd))
    if not path.endswith(_DRAFT_EXT) or not any(_strictly_under(path, os.path.realpath(s)) for s in ctx.scratch):
        return False
    # review: $? and 2>&1 only as whole words (d$?.txt is d0.txt to bash); any ( or ) fails closed (extglob's @(…))
    rest = re.sub(r"(?<!\S)(?:\$\?|2>&1)(?!\S)", "", _drop_data_heredocs(cmd).replace(line, "", 1))
    if "<<" in rest or re.search(r"[*?\[\]{}$`\\'\"()]|\balias\b|\bfunction\b|\bshopt\b|(?:^|[\s;&|])(?:cd|pushd)\b",
                                 rest):
        return False
    name = re.compile(rf"(?<![\w.-]){re.escape(os.path.basename(path))}(?![\w.-])")
    for seg in re.split(r"[;&|()\n]", rest):
        if not name.search(seg):
            continue
        use = _DRAFT_USE.fullmatch(seg[:name.search(seg).start()] + " ")
        script = use and use.group(1) and os.path.join(ctx.cwd, os.path.expanduser(use.group(1)))
        if not use or script and (os.path.islink(script) or not os.path.isfile(script)
                                  or os.path.realpath(script) == path or name.search(use.group(1))):
            return False
    return True


def _run_dir(starter, cwd):
    """The folder `python -` runs in (its sys.path[0]) after the starter's `cd` chain, or None when it can't be known."""
    d = cwd
    for target in re.findall(r"\bcd\s+([\w./~@+:=,-]+)\s*&&", starter):
        target = os.path.expanduser(target)
        d = target if os.path.isabs(target) else os.path.join(d, target) if d else None
    return d


# the cd chain is bounded (unbounded, each separator rescans the chain: quadratic); a longer chain is matched from a
# later link, and the cd before it makes the run folder unknown in _drives_fm
_PY_C = lazy(r"(?:^|(?<=[\n;&|(]))[ \t]*((?:cd\s+[\w./~@+:=,-]+\s*&&\s*){0,8}(?:/[\w./-]*/)?python[0-9.]*\s+"
                   r"(?:-(?:[bBdEIOPqsSuvV]+|[WX]\s*[\w.:=-]+)\s+)*-[bBdEIOPqsSuvV]*c\s+)"  # not -i: stdin runs after
                   r"""('[^']*'|"(?:[^"\\$`]|\\[\s\S])*")(?=\s|$|[;&|)])""")
_CD = lazy(r"(?:^|[\s;&|(])(?:cd|pushd|popd)\b")


def _python_c_units(shell):
    """T-1194: (the code of each `python -c CODE` that starts a command at shell top level, CODE one literal word with
    no expansion, after a chain of plain `cd DIR &&`; the shell with those words emptied; their starters). Nothing
    when the quote scan could part from bash's: a heredoc, $'…', a comment, an unclosed quote, a shell function or
    alias that could stand for python, ${…} (a ; in it is a word's text), python's own settings (what its imports load)."""
    if "<<" in shell or "$'" in shell or "${" in shell or "PYTHON" in shell or any(
            os.environ.get(k) for k in _PY_PATH_VARS) or re.search(
            r"\balias\b|\bfunction\b|python[0-9.]*\s*\(\s*\)", shell):
        return [], shell, []
    top, q, esc = [], None, False  # top[i]: shell[i] is outside every quote and escape (_quote_at, in one pass)
    for ch in shell:
        top.append(q is None and not esc)
        if esc:
            esc = False
        elif ch == "\\" and q != "'":
            esc = True
        elif q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
    if q or esc or any(ch == "#" and top[i] and (i == 0 or shell[i - 1] in " \t\n;&|(") for i, ch in enumerate(shell)):
        return [], shell, []
    units, starters, out, last = [], [], [], 0
    for m in _PY_C.finditer(shell):
        if not (top[m.start()] and (m.start() == 0 or top[m.start() - 1])):  # x\;python3 is one word, not a command
            continue
        word = m.group(2)[1:-1]
        units.append(word if m.group(2)[0] == "'" else
                     re.sub(r'\\(\n|[$`"\\])', lambda e: "" if e.group(1) == "\n" else e.group(1), word))
        starters.append(m.group(1))
        out.append(shell[last:m.start(2)] + "''")
        last = m.end()
    return units, "".join(out) + shell[last:], starters


def _drives_fm(cmd, cwd=None, cache=None):
    """T-0170: interpreter code that imports Sampo's modules and calls their writers. Quoted heredocs fed straight to
    python, and (T-1194) literal `python -c` code, are parsed: text that merely mentions `import fm…` (an edit script's
    strings) isn't an import. The text match of before decides for everything else: fm import text outside those
    (data heredocs, the shell, -c code it can't read), code that doesn't parse, and dynamic code (exec, eval, getattr,
    importlib…). It only relaxes what it can prove. T-2068: the result is the clause that fired, '' when none did, so the
    refusal can say which one (42 refusals in a week named none).
    T-4906: parsed code is also judged by the writer index (_fm_use_refused), with or without the text gate; `cache`
    is where the index is kept (Sampo's state folder)."""
    if not _FM_INTERNALS.search(_nfkc(cmd)):
        return ""
    try:
        idx = _writer_index(cache=cache)
    except Exception:  # an index that can't be built: the hand list alone, as before it
        idx = None
    # T-4906: text it can't read as python is judged by every name the index knows writes (`wide`, any module's: a name
    # alone can't say which); parsed code by the index itself, the hand list's reading (`gate`) and its dynamic names
    gate = _fm_gate(cmd)
    wide = gate or bool(idx) and _fm_gate(cmd, idx)
    units, rest, starters, _ = _python_units(cmd)
    c_units, shell, c_starters = _python_c_units("\n".join(rest))
    if wide and _FM_INTERNALS.search(_nfkc("\n".join([shell] + starters))):
        return "shell text it can't read as python names Sampo's modules (a heredoc not fed to python, a file run later)"
    # T-1204: python's own settings (PYTHONPATH loads sitecustomize, PYTHONSTARTUP…), set in the command or inherited,
    # change what any of its code runs, as _python_c_units already refuses for -c
    if wide and (units or c_units) and ("PYTHON" in "\n".join([shell] + starters)
                                        or any(os.environ.get(k) for k in _PY_PATH_VARS)):
        return "a PYTHON setting (PYTHONPATH, PYTHONSTARTUP…) changes what its python loads"
    # python - and -c import from the folder they run in: a cd anywhere but in their own `cd DIR &&` chain (an earlier
    # line, a `;`) makes that folder unknown (T-1204: heredocs too)
    moved = len(_CD.findall("\n".join([shell] + starters))) > sum(len(_CD.findall(s)) for s in starters + c_starters)
    runs = [None if moved else _run_dir(s, cwd) for s in starters + c_starters]
    import ast
    for code, run in zip(units + c_units, runs):
        if not _FM_INTERNALS.search(_nfkc(code)):
            continue
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError):
            if wide:
                return "python code that doesn't parse, so its imports can't be read"
            continue
        hit = _fm_use_refused(tree, idx) if idx else ""
        if not (gate or hit or idx and _dynamic_use(tree)):
            continue
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        modules = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + \
                  [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        odd = sorted(names & _DYNAMIC) + sorted(x for x in names if x.startswith("__")) + \
            [m for m in modules if m.split(".")[0] not in _PLAIN_IMPORTS and not m.startswith(FM_MODULE_PREFIXES)]
        if odd:  # its strings could still run: a process, a module it wrote, pickle, dunder walking…
            return f"dynamic code or an import outside re, json, textwrap and the like ({_ident(odd[0])})"
        # review (T-0286): python - (and -c) imports from its folder first — a json.py there runs
        if modules and run is None:
            return ("python's run folder is unknown (a cd outside its own `cd DIR && python` chain), so an import may "
                    "load a file there")
        shadow = [m for m in modules for x in (".py", "") if os.path.exists(os.path.join(run, m.split(".")[0] + x))]
        if modules and shadow:
            return f"python's run folder holds a file named like its import ({_ident(shadow[0])})"
        fm_from = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                   and (n.module or "").startswith(FM_MODULE_PREFIXES)]
        fm_import = [a for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
                     if a.name.startswith(FM_MODULE_PREFIXES)]
        # T-4893: one rule for both import forms (a from-import was refused even when the code only read): a writer or
        # entry called in the text, or reached by a name the ast reads (an import list, an attribute)
        if (fm_from or fm_import) and (_FM_WRITES.search(_nfkc(code)) or _fm_writer_named(tree)):
            return f"it imports {_ident((fm_from[0].module if fm_from else fm_import[0].name) or 'fm')}"
        if hit:  # T-4906: what the hand list missed, by the index (the name said)
            return f"it uses {hit}"
    return ""


def _ident(x):
    """A module or builtin name as a refusal may quote it: an identifier path, capped (never command text)."""
    m = re.match(r"[\w.]{1,40}", str(x))
    return m.group(0) if m else "?"


def _interp_code(cmd):
    """What the interpreter claude-check reads: the whole command but the `--run "<cmd>"` arguments of real fm calls,
    which check_bash reads on their own (interpreter checks included). A span is left out only when it starts at shell
    top level, outside heredoc bodies, in a simple command whose word is fm; heredoc bodies are read whole. T-0144: the
    T-0135 version stripped every match, so a fake `--run "` inside heredoc or -c code swallowed the call after it, and
    --run handed to the interpreter itself hid its argument. Anything uncertain is kept, so mistakes fail closed."""
    shell, bodies = _heredocs(cmd)
    if "$'" in shell or len(re.findall(r"(?<!<)<<(?!<)", shell)) != len(re.findall(_HEREDOC_START, shell)):
        return cmd  # $'…' quoting, or a heredoc form _heredocs doesn't know (<<\EOF): the scan can't follow, read all
    out, last = [], 0
    for m in _FM_RUN_ARG.finditer(shell):
        before = shell[:m.start()]
        if not _top_level(before):
            continue
        argv, _ = _strip_wrappers(_tokens(re.split(r"[;&|()\n`]", before)[-1]))
        name = os.path.basename(argv[0]) if argv else ""
        if name in FM_PROGRAMS or (re.match(r"^python[0-9.]*$", name) and argv[1:2] and argv[1].endswith(FM_SCRIPT_ENDS)):
            out.append(shell[last:m.start()] + " ")
            last = m.end()
    return "".join(out) + shell[last:] + "\n" + bodies


# T-0345: python calls that start a program, by module: where its command is (an argument index, or "k:" for the
# positional arguments from k on as the argv)
_SPAWN = {"subprocess": dict.fromkeys(("run", "call", "check_call", "check_output", "Popen", "getoutput",
                                       "getstatusoutput"), 0),
          "os": {"system": 0, "popen": 0, **dict.fromkeys(("execv", "execve", "execvp", "execvpe", "posix_spawn",
                                                           "posix_spawnp"), 1),
                 **dict.fromkeys(("execl", "execle", "execlp", "execlpe"), "1:"),
                 **dict.fromkeys(("spawnv", "spawnve", "spawnvp", "spawnvpe"), 2),
                 **dict.fromkeys(("spawnl", "spawnle", "spawnlp", "spawnlpe"), "2:")},
          "pty": {"spawn": 0}}


def _python_sources(cmd):
    """T-0345: the python code a command may run: every heredoc body (one that parses as python is read as python) and
    the code of each `python -c` (option clusters such as -uc included)."""
    return [src for src, _ in _python_sources_argv(cmd)]


def _py_runs(cmd):
    """T-4736: [(code, words, from_stdin)] for each python command in cmd: its `-c` code with the words after it (what
    sys.argv holds beyond argv[0]), or (None, the words after a bare `-` or [] for none, True) for one that reads its
    code from stdin, or (None, None, False) for a script file. words is None where the command takes more than it shows
    (xargs adds from its input). A stdin reader's flag is True only when its one stdin source is the one heredoc
    redirect on its own command (no pipe into it, no `< file`, no here-string, no second heredoc): else "mixed"."""
    out = []
    for c in _split(_tokens(_lines(_strip_heredocs(cmd)))):
        argv, xargs = _strip_wrappers(c.argv)
        if not (argv and re.match(r"^python[0-9.]*$", os.path.basename(argv[0]))):
            continue
        args, at = argv[1:], _shell_c_at(argv[1:])
        for i in at:
            out.append((args[i], None if xargs else args[i + 1:], False))
        if at:
            continue
        k = next((i for i, a in enumerate(args) if not a.startswith("-") or a == "-"), None)
        if k is None or args[k] == "-":
            sole = len(c.feeds) == 1 and c.feeds[0][0] == "<<" and not c.piped and c.stdin is None and not c.procsub
            out.append((None, None if xargs else args[k + 1:] if k is not None else [], True if sole else "mixed"))
        else:
            out.append((None, None, False))
    return out


def _python_sources_argv(cmd):
    """(source, argv words after it or None when unknown) for each python source of cmd (T-4736: what sys.argv holds
    beyond argv[0]: the words after the code of a `python -c`, after a bare `-` for a heredoc)."""
    out, lines, seen, i, bodies = [], cmd.split("\n"), [], 0, []
    while i < len(lines):
        line = lines[i]
        i += 1
        starts = _heredoc_starts(seen, line)
        seen.append(line)
        for _, delim in starts:
            body = []
            while i < len(lines) and not _ends_body(lines[i], delim, line):
                body.append(lines[i])
                i += 1
            i += 1
            bodies.append("\n".join(body))
    runs = _py_runs(cmd)
    readers = [(words, stdin) for code, words, stdin in runs if stdin]
    if len(bodies) == 1 and len(readers) == 1 and readers[0][1] is True:  # the one case with one answer: its body, its words
        out.append((bodies[0], readers[0][0]))
    else:  # two heredocs, a heredoc another command reads, a pipe, `< file`, a here-string: the words aren't known
        out += [(b, None) for b in bodies]
    out += [(code, words) for code, words, _ in runs if code is not None]
    return out


_ARGV_UNK = "\x02argv"  # _spawned_commands' answer for a command taken from argv the guard can't read (T-4736)
_RUNNERS = frozenset("sh bash dash zsh ksh csh fish env xargs sudo doas nice nohup timeout time command exec setsid stdbuf "
                     "ionice busybox".split())  # programs that run the words after them as a command (T-4736 review)
_CODE_RUNNERS = lazy(r"(?:python|pypy|perl|ruby|php|lua|node|nodejs|deno|bun|Rscript|julia)[0-9.]*$")
_CODE_FLAGS = frozenset(("-c", "-e", "-E", "-r", "-p", "-pe", "-ne", "eval", "--eval", "--print"))
_ARGV_BAD = lazy(r"[$`*?\[\]{}\x00]|^~")  # a word the outer shell may rewrite before the code sees it


def _argv_clean(ws):
    """ws, when it holds words the guard can be sure of: some, none of them one the shell expands ($x, `…`, a glob, ~);
    else None (unknown: xargs, a script file, a computed word)."""
    return ws if ws and not any(_ARGV_BAD.search(w) for w in ws) else None


def _spawned_commands(code, argv=False):
    """T-0345: the shell commands python code starts (subprocess, os.system/popen/exec*/spawn*, pty.spawn, under any
    import alias), as text check_bash reads: a literal string as it stands, a literal list with its words quoted, any
    other part as an unknown "$_" (so a delete of a variable is a delete of an unknown target), cwd= as a cd first.
    T-4736: a command taken from sys.argv (`sys.argv[1]`, `sys.argv[1:]`, a name set once from one) is the outer command
    line's own words, which `argv` holds (False: not asked, as for a script file; None: not known): read as the command
    those words are, or _ARGV_UNK when they can't be read (the same verdict node's process.argv gets)."""
    import ast
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return []
    alias = {}  # local name → (module, None) for the module, (module, function) for a from-import
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            alias.update({a.asname or a.name: (a.name, None) for a in n.names if a.name in _SPAWN})
        elif isinstance(n, ast.ImportFrom) and n.module in _SPAWN:
            alias.update({a.asname or a.name: (n.module, a.name) for a in n.names if a.name in _SPAWN[n.module]})
    once = {}  # a name assigned in exactly one place → its value (a command held in a name, a list built first)
    if argv is not False:
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                once.setdefault(n.targets[0].id, []).append(n.value)
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor, ast.NamedExpr)):
                for t in ast.walk(n.target):
                    if isinstance(t, ast.Name):
                        once.setdefault(t.id, []).extend([None, None])  # rebound some other way: not "once"

    def named(e):
        for _ in range(4):
            if not (isinstance(e, ast.Name) and len(once.get(e.id, ())) == 1 and once[e.id][0] is not None):
                break
            e = once[e.id][0]
        return e

    def from_argv(e):
        """None for an expression with nothing of argv in it; else ("s", word) for sys.argv[N], ("l", words) for
        sys.argv[N:], ("?",) for any other use of argv or a word the guard can't read."""
        if argv is False:
            return None
        e = named(e)

        def has(x, d=0):  # argv in x, also through a name set once to something holding it (a = sys.argv; a[1:])
            for y in ast.walk(x):
                if isinstance(y, ast.Attribute) and y.attr == "argv" or isinstance(y, ast.Name) and y.id == "argv":
                    return True
                if d < 4 and isinstance(y, ast.Name) and (v := named(y)) is not y and has(v, d + 1):
                    return True
            return False
        if not has(e):
            return None
        s = e.slice if isinstance(e, ast.Subscript) else None
        full = ["-c"] + list(argv or ())
        if isinstance(e, ast.Subscript) and isinstance(e.value, (ast.Attribute, ast.Name)) and \
                (getattr(e.value, "attr", None) or getattr(e.value, "id", None)) == "argv":
            if isinstance(s, ast.Constant) and type(s.value) is int and 0 <= s.value < len(full) and _argv_clean(full[s.value:s.value + 1]):
                return ("s", full[s.value])
            if isinstance(s, ast.Slice) and s.upper is None and s.step is None and (
                    s.lower is None or isinstance(s.lower, ast.Constant) and type(s.lower.value) is int and s.lower.value >= 0):
                ws = full[s.lower.value if s.lower is not None else 0:]
                if argv is not None and _argv_clean(ws):
                    return ("l", ws)
        return ("?",)

    bad = []

    def word(e):
        e = named(e)
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            return shlex.quote(e.value)
        if (v := from_argv(e)) is not None:
            if v[0] == "s":
                return shlex.quote(v[1])
            bad.append(1)
        return '"$_"'

    def words(elts):
        parts = []
        for e in elts:
            if isinstance(e, ast.Starred) and (v := from_argv(e.value)) is not None and v[0] == "l":
                parts += [shlex.quote(w) for w in v[1]]
            elif isinstance(e, ast.Starred) and (v := from_argv(e.value)) is not None:
                bad.append(1)
                parts.append('"$_"')
            else:
                parts.append(word(e))
        return " ".join(parts)
    def known(e):
        """Whether e is a program or command the code spells out: a string literal, sys.executable, or a word of argv
        (from_argv judges those: _ARGV_UNK where it can't read it)."""
        if isinstance(e, ast.Starred):
            return False
        e = named(e)
        return (isinstance(e, ast.Constant) and isinstance(e.value, str)) or (
            isinstance(e, ast.Attribute) and e.attr == "executable") or (
            (v := from_argv(e)) is not None and v[0] == "s")

    def computed_call(n, mod, fn, where, arg):
        """T-4736 review: whether the command this call starts is computed (an f-string, a name from input(), a sum),
        so what runs can't be read from the code: a string command in any part; a list's program word, or any word of a
        list run by a shell (shell=True, sh -c, env, xargs …); an exec/spawn's path."""
        if mod == "os" and fn not in ("system", "popen"):  # exec*/spawn*/posix_spawn*: the path comes before the argv
            k = (int(where[:-1]) if isinstance(where, str) else where) - 1
            path = n.args[k] if len(n.args) > k else next((x.value for x in n.keywords if x.arg in ("path", "file")), None)
            return path is None or isinstance(path, ast.Starred) or not known(path)
        if isinstance(where, str) or any(isinstance(a, ast.Starred) for a in n.args[:where + 1]):
            return True
        arg = named(arg)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return False
        if not isinstance(arg, (ast.List, ast.Tuple)):
            return from_argv(arg) is None  # a name, f-string, sum, call: computed (argv shapes are judged by `bad`)
        if not arg.elts or not known(arg.elts[0]):
            return True
        first = named(arg.elts[0])
        shell = any(k.arg == "shell" and not (isinstance(k.value, ast.Constant) and not k.value.value) for k in n.keywords)
        if shell or (isinstance(first, ast.Constant) and os.path.basename(first.value) in _RUNNERS):
            return any(not known(e) for e in arg.elts)
        if isinstance(first, ast.Constant) and _CODE_RUNNERS.match(os.path.basename(first.value)):  # python3 -c CODE
            flag = next((i for i, e in enumerate(arg.elts) if isinstance(e, ast.Constant) and e.value in _CODE_FLAGS), None)
            return flag is not None and any(not known(e) for e in arg.elts[flag + 1:])
        return False
    out = []
    for n in ast.walk(tree):
        f = getattr(n, "func", None) if isinstance(n, ast.Call) else None
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and alias.get(f.value.id, (0, 0))[1] is None:
            mod, fn = alias[f.value.id][0], f.attr
        elif isinstance(f, ast.Name) and alias.get(f.id, (0, None))[1]:
            mod, fn = alias[f.id]
        else:
            continue
        where = _SPAWN[mod].get(fn)
        if where is None:
            continue
        bad.clear()
        if isinstance(where, str):
            text = words(n.args[int(where[:-1]):])
        else:
            arg = n.args[where] if len(n.args) > where else next(
                (k.value for k in n.keywords if k.arg in ("args", "argv", "cmd", "command")), None)
            if arg is None:
                continue
            arg = named(arg)
            v = from_argv(arg)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                text = arg.value
            elif isinstance(arg, (ast.List, ast.Tuple)):
                text = words(arg.elts)
            elif v is not None and v[0] == "s":
                text = v[1]  # one word of argv as the shell text (or the program) the code starts
            elif v is not None and v[0] == "l":
                text = " ".join(shlex.quote(w) for w in v[1])
            else:
                text = '"$_"'
                bad += [1] if v is not None else []
        cwd = next((k.value for k in n.keywords if k.arg == "cwd"), None)
        pre = f"cd {word(cwd)} && " if cwd is not None else ""
        out.append(pre + text)  # an unreadable argv word stands as "$_" here: what that already refuses keeps its reason
        if bad or argv is not False and computed_call(n, mod, fn, where, arg if not isinstance(where, str) else None):
            out.append(_ARGV_UNK)  # (a script file, argv False, keeps the old policy: a computed command is let go)
    return out


# T-4673: system paths written from interpreter code. The coarse scan above reads every quoted path of a script that
# writes anything, so it is kept to the guarded categories (a script that merely reads /etc would be refused). A system
# path is judged only where a write call really takes it: python through its parse tree, the other languages through
# a table of their write calls (name, receiver, which argument is the target), each target one the code spells out
# (a literal, a literal joined to others, a name bound once to one). Reads, content arguments and data stay free.
_SYS_GATE = lazy(r"\b(?:open|fopen|sysopen|ZipFile|connect|os\.|shutil|unlink|rm|rmdir|mkdir|rename|replace|write\w*|append\w*"
                 r"|copy\w*|cp\w*|mv|move|link|symlink\w*|touch|chmod\w*|chown\w*|remove\w*|delete|truncate|utime\w*|create\w*"
                 r"|file(?:[_.(=]|[ \t]+\w|[ \t]+=)|make_path|mkpath|sink|save\w*|put_contents|output|install|ln\w*|sync\w*"
                 r"|dir\.create|__import__|import_module|getattr\s*\("
                 r"|extract\w*)")  # a stem: rm_f; (T-4673 review: no bare os, dir, set, file: words of every script)
_PY_MODS = frozenset("os shutil io builtins codecs pathlib gzip bz2 lzma tarfile zipfile sqlite3".split())
_PY_PATHS = frozenset("Path PurePath PosixPath PurePosixPath".split())
_PY_CALLS = {
    "os": {**dict.fromkeys(("remove unlink rmdir removedirs mkdir makedirs truncate chmod chown lchown lchmod chflags "
                            "lchflags utime mkfifo mknod setxattr removexattr chroot").split(), "first"),
           **dict.fromkeys(("rename replace renames").split(), "both"), **dict.fromkeys(("symlink link").split(), "second")},
    "shutil": {**dict.fromkeys(("rmtree make_archive chown").split(), "first"),
               **dict.fromkeys(("copy copy2 copyfile copytree copymode copystat unpack_archive").split(), "second"),
               "move": "both"},
    "sqlite3": {"connect": "first"}, "zipfile": {"ZipFile": "zip"}}
_PY_RECV_WRITES = frozenset("write_text write_bytes touch unlink mkdir rmdir chmod lchmod symlink_to hardlink_to".split())
_PY_WRITE_FLAGS = frozenset("O_WRONLY O_RDWR O_CREAT O_APPEND O_TRUNC O_EXCL".split())
_PY_PASS = frozenset("resolve absolute expanduser as_posix expandvars".split())
_PY_PASS_FN = frozenset("str fspath abspath normpath realpath expanduser expandvars".split())
_FORMAT_FIELD = lazy(r"\{(\w*)(?:[:!][^}]*)?\}")
_PERCENT_FIELD = lazy(r"%[-#0 +]*\d*(?:\.\d+)?[sdrifx]")


def _py_few(items, n):
    """items cut to n; a cut leaves the unknown "\\x01" in the last place (fails closed: a value the guard didn't read
    may be a system path, and _interp_system_writes judges one as system), as a count of write calls past the cap does"""
    return items if len(items) <= n else items[:n - 1] + [("\x01", False)]


def _py_dyn_module(e):
    """The module name `__import__('os')` or `importlib.import_module('os')` gives (T-4686), else ''."""
    import ast
    if isinstance(e, ast.Call) and e.args and isinstance(e.args[0], ast.Constant) and isinstance(e.args[0].value, str):
        f = e.func
        if (isinstance(f, ast.Name) and f.id in ("__import__", "import_module")) or (
                isinstance(f, ast.Attribute) and f.attr == "import_module"):
            return e.args[0].value.split(".")[0]
    return ""


def _py_system_targets(code):
    """T-4673: every path a python source passes to a call that writes, deletes, moves to, links or changes it (open
    with a write mode, os/shutil/pathlib writers, the archive and database openers), as text; raises SyntaxError on code
    that isn't python. A path is a literal or one the code builds from literals (names bound in the code, + / %, f-strings,
    .format, Path(), os.path.join, .parent): each reading a name has counts; a part the code doesn't give is "\\x00" and a
    path that starts with one is left out. ponytail: a value from the environment, a call or a file isn't followed."""
    import ast
    tree = ast.parse(code)
    nodes = list(ast.walk(tree))
    mods, fns, binds = {}, {}, {}
    for n in nodes:
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in _PY_MODS:
                    mods[a.asname or a.name.split(".")[0]] = a.name.split(".")[0] if not a.asname else a.name
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in _PY_MODS:
            for a in n.names:
                fns[a.asname or a.name] = (n.module, a.name)
    paths = _PY_PATHS | {k for k, (m, f) in fns.items() if m == "pathlib" and f in _PY_PATHS}

    def bind(name, e):
        binds.setdefault(name, []).append(e)
    for n in nodes:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    bind(t.id, n.value)
                elif isinstance(t, (ast.Tuple, ast.List)) and isinstance(n.value, (ast.Tuple, ast.List)) \
                        and len(t.elts) == len(n.value.elts):
                    for a, b in zip(t.elts, n.value.elts):
                        if isinstance(a, ast.Name):
                            bind(a.id, b)
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and n.value is not None and isinstance(n.target, ast.Name):
            bind(n.target.id, n.value)
    # T-4686: a function's parameters are what its calls pass (and its defaults); a loop or comprehension target is each
    # element of what it walks; `m = __import__('os')` makes m the module, as an import does
    funcs, methods, classes = {}, {}, {}
    for n in nodes:
        if isinstance(n, ast.ClassDef):
            for b in n.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.setdefault(b.name, []).append(b)
                    if b.name == "__init__":
                        classes.setdefault(n.name, []).append(b)
    method_ids = {id(b) for bs in methods.values() for b in bs}
    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if id(n) not in method_ids:
                funcs.setdefault(n.name, []).append(n)
        elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Lambda):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    funcs.setdefault(t.id, []).append(n.value)
        elif isinstance(n, ast.Assign) and (dm := _py_dyn_module(n.value)):
            for t in n.targets:
                if isinstance(t, ast.Name) and dm in _PY_MODS:
                    mods[t.id] = dm
        elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) \
                and n.value.func.id == "getattr" and len(n.value.args) == 2 and isinstance(n.value.args[0], ast.Name) \
                and n.value.args[0].id in mods and isinstance(n.value.args[1], ast.Constant) \
                and isinstance(n.value.args[1].value, str):  # g = getattr(os, 'remove'): g is os.remove
            for t in n.targets:
                if isinstance(t, ast.Name):
                    fns[t.id] = (mods[n.value.args[0].id], n.value.args[1].value)
    # a parameter is what its calls pass; one with no call the scan can match to the arguments (a call through *a or **k,
    # a function handed on to map or partial, a lambda used as a callback, a method no call names) is unknown, whatever
    # a default says: only a def that holds a call can reach a write
    unknown_v = ast.Constant(value="\x01\x01")  # T-4736: two marks: a parameter no call binds (its own reason, _UNBOUND)
    defs = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            and any(isinstance(x, ast.Call) for x in ast.walk(n))]
    called, passed_on, call_funcs = set(), set(), set()

    def all_params(fd):
        a = fd.args
        return a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]

    def feed(fd, call, skip):
        called.add(id(fd))
        a = fd.args
        if any(isinstance(v, ast.Starred) for v in call.args) or any(k.arg is None for k in call.keywords):
            passed_on.add(id(fd))  # the arguments aren't ones the scan can match
            return
        for p, v in zip((a.posonlyargs + a.args)[skip:], call.args):
            bind(p.arg, v)
        names = {x.arg for x in a.args + a.kwonlyargs}
        for k in call.keywords:
            if k.arg in names:
                bind(k.arg, k.value)

    def skip_of(fd):  # self or cls first, unless a staticmethod
        return 0 if any(isinstance(x, ast.Name) and x.id == "staticmethod" for x in fd.decorator_list) else 1

    def elems(it, d=0):
        """[expression] each element of the iterable `it` may be (None: one it can't follow)."""
        if d > 4:
            return None
        if isinstance(it, (ast.List, ast.Tuple, ast.Set)):
            return list(it.elts)
        if isinstance(it, ast.Name):
            got = [elems(b, d + 1) for b in binds.get(it.id, ())]
            return None if not got or None in got else [x for g in got for x in g]
        if isinstance(it, ast.Call):
            f = it.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("sorted", "list", "set", "tuple", "reversed", "iter", "frozenset") and it.args:
                return elems(it.args[0], d + 1)
            if name in ("glob", "iglob") and it.args and (isinstance(f, ast.Name) or (
                    isinstance(f.value, ast.Name) and f.value.id == "glob")):  # a name the pattern matches
                return [ast.BinOp(left=it.args[0], op=ast.Add(), right=ast.Constant(value="\x00"))]
            if name in ("listdir", "scandir") and it.args:  # a name in the folder
                return [ast.BinOp(left=it.args[0], op=ast.Add(), right=ast.Constant(value="/\x00"))]
            if name in ("iterdir", "glob", "rglob") and isinstance(f, ast.Attribute):  # Path(d).glob(p): d/<name>
                return [ast.BinOp(left=f.value, op=ast.Div(), right=ast.Constant(value="\x00"))]
        return None

    def feed_map(fd, call):
        """T-4736: map(f, xs), pool.map(f, xs), executor.map(f, xs, ys): f's parameters are what xs, ys hold, one iterable
        to each parameter (every element to each: a superset of what map passes). False when an iterable is one the scan
        can't follow, or f takes the elements through *a (the parameters stay unknown)."""
        a = fd.args
        params = a.posonlyargs + a.args
        its = [elems(x) for x in call.args[1:] if not (isinstance(x, ast.Constant) and isinstance(x.value, int))]  # chunksize
        if not its or None in its or a.vararg or a.kwarg or len(its) > len(params):
            return False
        called.add(id(fd))
        for p, got in zip(params, its):
            for elt in got:
                bind(p.arg, elt)
        return True

    mapped = set()  # the Name nodes of a function map takes (their reads aren't a hand-over to unknown callers)
    for n in nodes:
        if isinstance(n, ast.Call):
            f = n.func
            call_funcs.add(id(f))
            mname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if mname in ("map", "imap", "imap_unordered") and len(n.args) >= 2 \
                    and mname not in funcs and mname not in methods and mname not in binds \
                    and not any(isinstance(x, ast.Starred) for x in n.args) \
                    and (isinstance(n.args[0], ast.Lambda) or (
                        isinstance(n.args[0], ast.Name) and len(funcs.get(n.args[0].id, ())) == 1
                        and n.args[0].id not in classes)) \
                    and feed_map(n.args[0] if isinstance(n.args[0], ast.Lambda) else funcs[n.args[0].id][0], n):
                mapped.add(id(n.args[0]))
            if isinstance(f, ast.Name):
                for fd in funcs.get(f.id, ()):
                    feed(fd, n, 0)
                for fd in classes.get(f.id, ()):
                    feed(fd, n, 1)
            elif isinstance(f, ast.Attribute):
                for fd in methods.get(f.attr, ()):
                    feed(fd, n, skip_of(fd))
            elif isinstance(f, ast.Lambda):
                feed(f, n, 0)
    for n in nodes:  # a name or attribute that is no call target: the function it names may be called with anything
        if isinstance(n, ast.Name) and id(n) not in call_funcs and id(n) not in mapped and isinstance(n.ctx, ast.Load):
            passed_on.update(id(fd) for fd in funcs.get(n.id, []) + classes.get(n.id, []))
        elif isinstance(n, ast.Attribute) and id(n) not in call_funcs and n.attr in methods:
            passed_on.update(id(fd) for fd in methods[n.attr])
    for fd in defs:
        if id(fd) not in called or id(fd) in passed_on:
            for p in all_params(fd):
                bind(p.arg, unknown_v)
        a = fd.args
        for p, v in list(zip(reversed(a.posonlyargs + a.args), reversed(a.defaults))) + [
                (p, v) for p, v in zip(a.kwonlyargs, a.kw_defaults) if v is not None]:
            bind(p.arg, v)

    for n in nodes:
        if isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            got = elems(n.iter)
            if got is None:
                continue
            for elt in got:
                if isinstance(n.target, ast.Name):
                    bind(n.target.id, elt)
                elif isinstance(n.target, (ast.Tuple, ast.List)) and isinstance(elt, (ast.Tuple, ast.List)) \
                        and len(n.target.elts) == len(elt.elts):
                    for a, b in zip(n.target.elts, elt.elts):
                        if isinstance(a, ast.Name):
                            bind(a.id, b)

    def join(a, b):
        return os.path.join(a, b)

    few, memo, busy = _py_few, {}, set()

    def val(e, d=0):
        """[(text, is_a_path_object)] the expression may be."""
        if d > 8:  # a value built through more steps than are followed: the unknown, a path object (fails closed)
            return [("\x01", True)]
        if isinstance(e, ast.Constant):
            if isinstance(e.value, str):
                return [(e.value, False)]
            return [(e.value.decode("utf-8", "replace"), False)] if isinstance(e.value, bytes) else []
        if isinstance(e, ast.Name):
            if e.id in memo:
                return memo[e.id]
            if e.id in busy:  # a = a + "x": the other bindings give the value
                return []
            busy.add(e.id)  # every binding of the name counts (T-4673 review: four hid a fifth); each name read once
            memo[e.id] = few([v for b in binds.get(e.id, ()) for v in val(b, d + 1)], 64)
            busy.discard(e.id)
            return memo[e.id]
        if isinstance(e, ast.IfExp):
            return few(val(e.body, d + 1) + val(e.orelse, d + 1), 16)
        if isinstance(e, ast.BoolOp):
            return few([v for x in e.values for v in val(x, d + 1)], 16)
        if isinstance(e, ast.BinOp) and isinstance(e.op, (ast.Add, ast.Div, ast.Mod)):
            left = val(e.left, d + 1)
            if isinstance(e.op, ast.Mod):
                return [(_PERCENT_FIELD.sub("\x00", s), p) for s, p in left]
            right = few(val(e.right, d + 1) or [("\x00", False)], 3)
            if isinstance(e.op, ast.Div):
                return few([(join(a, b), True) for a, _ in left for b, _ in right], 16)
            return few([(a + b, p) for a, p in left for b, _ in right], 16)
        if isinstance(e, ast.JoinedStr):
            cands = [""]
            for v in e.values:
                opts = [v.value] if isinstance(v, ast.Constant) else (
                    [s for s, _ in few(val(v.value, d + 1), 3)] or ["\x00"])
                cands = [a + b for a in cands for b in opts if isinstance(b, str)]
                cands = cands if len(cands) <= 16 else cands[:15] + ["\x01"]
            return [(c, False) for c in cands]
        if isinstance(e, ast.Attribute) and e.attr == "parent":
            return [(os.path.dirname(s.rstrip("/")) or "/", True) for s, _ in val(e.value, d + 1)]
        if isinstance(e, ast.Call):
            f = e.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in paths and (isinstance(f, ast.Name) or isinstance(f.value, ast.Name)):
                return _py_join(e.args, val, join, d, ".", True)
            if name == "join" and isinstance(f, ast.Attribute) and (
                    getattr(f.value, "attr", getattr(f.value, "id", "")) in ("path", "posixpath")):
                return _py_join(e.args, val, join, d, "", False)
            if name == "joinpath" and isinstance(f, ast.Attribute):
                return _py_join([f.value] + e.args, val, join, d, ".", True)
            if isinstance(f, ast.Attribute) and name in _PY_PASS:
                return val(f.value, d + 1)
            if name in _PY_PASS_FN and e.args:
                return val(e.args[0], d + 1)
            if name in ("dirname",) and e.args:
                return [(os.path.dirname(s), False) for s, _ in val(e.args[0], d + 1)]
            if name == "format" and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Constant) \
                    and isinstance(f.value.value, str):
                auto = iter(range(99))

                def field(m):
                    k = m.group(1)
                    i = int(k) if k.isdigit() else next(auto) if not k else -1
                    arg = (e.args[i] if 0 <= i < len(e.args) else None) if i >= 0 else next(
                        (x.value for x in e.keywords if x.arg == k), None)
                    got = val(arg, d + 1) if arg is not None else []
                    return got[0][0] if got else "\x00"
                return [(_FORMAT_FIELD.sub(field, f.value.value), False)]
        return []

    def arg(n, i, *kws):
        if len(n.args) > i and not isinstance(n.args[i], ast.Starred):
            return n.args[i]
        return next((k.value for k in n.keywords if k.arg in kws), None)

    def writes_mode(e):  # a missing mode reads; one the code doesn't give is taken as a write
        if e is None:
            return False
        ms = [s for s, _ in val(e)]
        return not ms or any(set(s) & set("wax+\x01") for s in ms)

    def writes_flags(e):
        if e is None:
            return False
        if isinstance(e, ast.Constant) and isinstance(e.value, int):
            return bool(e.value & 0o3 or e.value & 0o3100 or e.value & 0o2000)
        names = {x.attr if isinstance(x, ast.Attribute) else x.id for x in ast.walk(e) if isinstance(x, (ast.Attribute, ast.Name))}
        known = {x for x in names if x.startswith("O_")}
        return bool(known & _PY_WRITE_FLAGS) or not known

    out = []

    def add(e, need_path=False):
        for s, p in val(e):
            if p or not need_path or "\x01" in s:  # the unknown counts whatever it was a value of
                out.append(s)

    def call_one(n, mod, fn, recv):
        if mod is not None:
            mod = mod.split(".")[0]
            if fn == "open" and mod in ("builtins", "io", "codecs", "gzip", "bz2", "lzma", "tarfile"):
                if writes_mode(arg(n, 1, "mode")):
                    add(arg(n, 0, "file", "filename", "name"))
            elif fn == "open" and mod == "os":
                if writes_flags(arg(n, 1, "flags")):
                    add(arg(n, 0, "path"))
            elif (kind := _PY_CALLS.get(mod, {}).get(fn)) == "zip":
                if writes_mode(arg(n, 1, "mode")):
                    add(arg(n, 0, "file"))
            elif kind in ("first", "both"):
                add(arg(n, 0, "path", "src"))
                if kind == "both":
                    add(arg(n, 1, "dst", "dest"))
            elif kind == "second":
                add(arg(n, 1, "dst", "dest", "link_name", "extract_dir", "base_name"))
        elif recv is not None:
            if fn in _PY_RECV_WRITES:
                add(recv)
            elif fn == "open":
                if writes_mode(arg(n, 0, "mode")):
                    add(recv, need_path=True)
            elif fn in ("rename", "replace"):
                kinds = [p for _, p in val(recv)]
                if any(kinds):
                    add(recv, need_path=True)
                    add(arg(n, 0, "target"))
            elif fn in ("extractall", "extract"):  # an archive's members land under the folder (none given: here)
                dest = arg(n, 0 if fn == "extractall" else 1, "path")
                if dest is not None:
                    add(dest)
                else:
                    out.append(".")

    def module_of(e):
        if isinstance(e, ast.Name) and e.id in mods:
            return mods[e.id].split(".")[0]
        return _py_dyn_module(e) if _py_dyn_module(e) in _PY_MODS else ""

    def getattr_call(e):  # getattr(<one of the modules>, name)
        return isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "getattr" \
            and len(e.args) >= 2 and bool(module_of(e.args[0]))
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        f, mod, fn, recv, calls = n.func, None, None, None, []
        if getattr_call(n):  # getattr(os, name): a name the code doesn't spell out may be any writer of the module
            if any(re.search(r"[\x00\x01]", s) or not s for s, _ in val(n.args[1]) or [("", 0)]):
                out.append("\x01")
        if isinstance(f, ast.Name):
            if f.id in fns:
                mod, fn = fns[f.id]
            elif f.id == "open" and f.id not in binds:
                mod, fn = "builtins", "open"
        elif isinstance(f, ast.Attribute):
            if mm := module_of(f.value):
                mod, fn = mm, f.attr
            else:
                recv, fn = f.value, f.attr
        elif getattr_call(f):  # getattr(os, 'remove')(p)
            calls = [(module_of(f.args[0]), s, None) for s, _ in val(f.args[1]) if not re.search(r"[\x00\x01]", s)]
        for c in calls or [(mod, fn, recv)]:
            call_one(n, *c)
        if len(out) > 256:  # past this the rest isn't read: that is judged as a path the guard can't rule out
            out.append("\x01")
            break
    return out


def _py_join(args, val, join, d, start, is_path):
    """The candidates of a path built from `args` (Path(a, b), os.path.join(a, b)): the first part must be known, a
    later one that isn't is "\\x00"."""
    if not args:
        return [(start, is_path)]
    cands = None
    for i, a in enumerate(args):
        got = val(a, d + 1)
        if not got:
            if i == 0:
                return []
            got = [("\x00", False)]
        got = _py_few(got, 3)
        cands = [(b, is_path) for b, _ in got] if cands is None else _py_few([
            (join(a0, b), is_path) for a0, _ in cands for b, _ in got], 16)
    return cands


# the other languages: (receivers that may precede the name, a bare call allowed, {name: which argument is the target})
# kinds: first, second, both, all (every argument), open (target first, mode second), plopen (perl's open), named (R)
_FS_NAMES = {**dict.fromkeys(("writeFile writeFileSync appendFile appendFileSync createWriteStream unlink unlinkSync rm "
                              "rmSync rmdir rmdirSync mkdir mkdirSync truncate truncateSync chmod chmodSync chown "
                              "chownSync lchown utimes utimesSync lutimes writeTextFile writeTextFileSync remove "
                              "removeSync create createSync write").split(), "first"),
             **dict.fromkeys("rename renameSync".split(), "both"),
             **dict.fromkeys(("copyFile copyFileSync cp cpSync symlink symlinkSync link linkSync copy copySync").split(),
                             "second"),
             "open": "open", "openSync": "open"}
_FILE_RB = {**dict.fromkeys("write binwrite".split(), "first"),
            **dict.fromkeys("delete unlink chmod chown lchmod lchown utime truncate mkfifo".split(), "all"),
            "rename": "both", "symlink": "second", "link": "second", "open": "open", "new": "open"}
_FILEUTILS = {**dict.fromkeys(("rm rm_f rm_r rm_rf remove remove_file remove_dir remove_entry remove_entry_secure rmdir "
                               "rmtree mkdir mkdir_p makedirs mkpath touch chmod chmod_R chown chown_R").split(), "all"),
              **dict.fromkeys("cp cp_r copy copy_file copy_entry install ln ln_s ln_sf link symlink".split(), "second"),
              "mv": "both", "move": "both"}
_PERL_NAMES = {**dict.fromkeys("unlink rmdir chmod chown utime make_path remove_tree mkpath rmtree".split(), "all"),
               **dict.fromkeys("mkdir truncate sysopen".split(), "first"), "rename": "both", "move": "both",
               "mv": "both", "symlink": "second", "link": "second", "copy": "second", "cp": "second", "open": "plopen"}
_PHP_NAMES = {**dict.fromkeys("file_put_contents unlink rmdir mkdir touch chmod chown chgrp".split(), "first"),
              "rename": "both", "copy": "second", "symlink": "second", "link": "second", "move_uploaded_file": "second",
              "fopen": "open"}
_R_NAMES = {**dict.fromkeys(("writeLines write.csv write.csv2 write.table saveRDS write writeBin file.copy file.symlink "
                             "file.link").split(), "second"),
            **dict.fromkeys("file.remove unlink dir.create file.create file.append sink Sys.chmod".split(), "all"),
            "file.rename": "both", "cat": "named", "save": "named", "file": "open"}
_JL_NAMES = {**dict.fromkeys("rm mkdir mkpath touch chmod chown write".split(), "first"), "mv": "both", "cp": "second",
             "symlink": "second", "open": "open"}
_PY_TEXT = [(frozenset(["os"]), False, {"remove": "first", "unlink": "first", "rmdir": "first", "mkdir": "first",
                                        "makedirs": "first", "chmod": "first", "rename": "both", "replace": "both",
                                        "symlink": "second", "link": "second", "truncate": "first"}),
            (frozenset(["shutil"]), False, {"copy": "second", "copy2": "second", "copyfile": "second",
                                            "copytree": "second", "move": "both", "rmtree": "first"}),
            (frozenset(["io"]), True, {"open": "open"})]
_TEXT_ROWS = {
    "js": [(frozenset("fs fse fsp fsPromises promises Deno Bun )".split()), False, _FS_NAMES),
           (frozenset(), True, {**{k: v for k, v in _FS_NAMES.items() if k.endswith("Sync")}, "createWriteStream": "first"})],
    "pl": [(frozenset(), True, _PERL_NAMES), (frozenset(["Copy", "Path"]), False, _PERL_NAMES)],
    "rb": [(frozenset(["File", "IO"]), False, _FILE_RB), (frozenset(["Dir"]), False, dict.fromkeys(
        "mkdir rmdir delete unlink".split(), "first")), (frozenset(["FileUtils"]), False, _FILEUTILS),
        (frozenset(), True, {"open": "open"})],  # Kernel#open("path", "w")
    "php": [(frozenset(), True, _PHP_NAMES)],
    "lua": [(frozenset(["io"]), False, {"open": "open", "output": "first"}),
            (frozenset(["os"]), False, {"remove": "first", "rename": "both"})],
    "r": [(frozenset(), True, _R_NAMES)],
    "jl": [(frozenset(), True, _JL_NAMES), (frozenset(["Base", "Filesystem"]), False, _JL_NAMES)],
    "py": _PY_TEXT}
_LANG_OF = {"node": "js", "nodejs": "js", "deno": "js", "bun": "js", "perl": "pl", "ruby": "rb", "jruby": "rb",
            "truffleruby": "rb", "irb": "rb", "php": "php", "lua": "lua", "luajit": "lua", "Rscript": "r", "R": "r", "julia": "jl",
            "tclsh": "tcl"}
_PAREN_LESS = frozenset(["pl", "rb"])  # a call without parentheses: unlink "x"; File.delete "x"
_R_PATH_ARGS = frozenset("file con path to dest destfile filename target".split())
_TEXT_WRITE_MODE = lazy(r"^[rwxabcest+]{0,4}$")
_TEXT_WRITE_FLAGS = lazy(r"\b(?:O_WRONLY|O_RDWR|O_CREAT|O_APPEND|O_TRUNC|WRONLY|RDWR|CREAT|APPEND|TRUNC|FILE_APPEND)\b|"
                         r"\b(?:write|append|create|truncate)\s*[:=]\s*true\b")
_T_QUOTED = lazy(r"""(?s)'([^'\\]*(?:\\.[^'\\]*)*)'|"([^"\\]*(?:\\.[^"\\]*)*)"|`([^`\\]*(?:\\.[^`\\]*)*)`""")
# (unrolled per quote: the one pattern `(['"`])((?:\\.|(?!\1).)*)\1` took exponential time on an unterminated quote
# followed by backslashes, T-4673 review; each backslash here is read one way only)
_T_INTERP = lazy(r"\$\{(\w+)\}|\$\((\w+)\)|\{\$(\w+)\}|#\{(\w+)\}|\$(\w+)|\$\{([^{}]+)\}|#\{([^{}]+)\}")  # (last two: an expression, bound to nothing = unknown)
_T_JOIN = lazy(r"(?:path\.(?:join|resolve)|Path\.of|Paths\.get|(?:File|Pathname)\.(?:join|expand_path)|joinpath|join|resolve"
               r"|file\.path|paste0|normalizePath)\s*\(")
_T_BIND = lazy(r"(?:^|[;{(\s,])(?:(?:const|let|var|my|our|local|val|set|final)\s+)?[$@]?([A-Za-z_]\w*)\s*"
               r"(?::=|<-|=)(?![=~>])\s*")
_T_NAME = lazy(r"[$@]?([A-Za-z_]\w*)(?=\s|$|[);,])")
_TCL_OPEN = lazy(r"""\bopen[ \t]+(\{[^}\n]*\}|"[^"\n]*"|[^\s\]\[;]+)(?:[ \t]+(\{[^}\n]*\}|"[^"\n]*"|[^\s\]\[;]+))?""")
_TCL_FILE = lazy(r"\bfile[ \t]+(delete|rename|mkdir|copy|link|attributes)\b([^\n;\]]*)")
_TCL_SET = lazy(r"\bset[ \t]+(\w+)[ \t]+(\{[^}\n]*\}|\"[^\"\n]*\"|\S+)")


_CUT = "'\x01'"  # the last argument _call_args gives when its window ended before the call did: a value not read


def _call_args(text, i, paren):
    """The top-level arguments of the call whose argument text starts at text[i] (just past "(" when `paren`, else the
    first word of a call written without parentheses, which ends at ; or a newline): quote- and depth-aware, bounded. A
    call longer than the window ends in _CUT (the unknown), so what lies past the window is judged, not dropped."""
    out, cur, depth, q, end = [], [], 0, "", min(len(text), i + 3000)
    cut = False  # (set by the while's else: the loop ran to its window's end, no ")" or ";" closed the call)
    while i < end:
        c = text[i]
        if q:
            cur.append(c)
            if c == "\\" and i + 1 < end:
                cur.append(text[i + 1])
                i += 1
            elif c == q:
                q = ""
        elif c in "'\"`":
            q = c
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
            cur.append(c)
        elif depth == 0 and c == ",":
            out.append("".join(cur))
            cur = []
        elif depth == 0 and not paren and c in ";\n":
            break
        else:
            cur.append(c)
        i += 1
    else:
        cut = end < len(text)
    out.append("".join(cur))
    return [a.strip() for a in out if a.strip()] + ([_CUT] if cut else [])


def _terms(e):
    """The pieces of a concatenation (a + b, a . b, a .. b, a * b), split outside quotes and brackets."""
    out, cur, depth, q, i = [], [], 0, "", 0
    while i < len(e):
        c = e[i]
        if q:
            cur.append(c)
            if c == "\\" and i + 1 < len(e):
                cur.append(e[i + 1])
                i += 1
            elif c == q:
                q = ""
        elif c in "'\"`":
            q = c
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif depth == 0 and (c in "+*" or e.startswith("..", i) or (
                c == "." and (e[i - 1:i] in " \t'\"`)") and e[i + 1:i + 2] in " \t'\"`$")):
            out.append("".join(cur))
            cur = []
            i += 1 if e.startswith("..", i) else 0
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [t for t in (x.strip() for x in out) if t]


def _t_cut(items, n):
    """items cut to n; a cut leaves the unknown "\\x01" last (a value the guard didn't read: judged as a system path)."""
    return items if len(items) <= n else items[:n - 1] + ["\x01"]


_T_ALT_MAX = 8


def _t_alts(e):
    """T-4686: the alternatives of a text-language expression that picks one: `c ? a : b`, `a || b`, `a ?? b`, `a // b`,
    `a ?: b`, with the outer parentheses of the whole expression peeled. [e] when it picks nothing; a
    condition (the piece before a `?`) is no alternative. More than _T_ALT_MAX pieces end in "\\x01" (the unknown)."""
    e = e.strip()
    while e.startswith("(") and e.endswith(")"):  # one pair around all of it, not `(a) + (b)`
        d = 0
        for i, c in enumerate(e):
            d += (c == "(") - (c == ")")
            if d == 0 and i < len(e) - 1:
                break
        else:
            e = e[1:-1].strip()
            continue
        break
    pieces, seps, cur, depth, q, i = [], [], [], 0, "", 0
    while i < len(e):
        c = e[i]
        if q:
            cur.append(c)
            if c == "\\" and i + 1 < len(e):
                cur.append(e[i + 1])
                i += 1
            elif c == q:
                q = ""
            i += 1
            continue
        sep = ""
        if c in "'\"`":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0:
            spaced = e[i - 1:i] in (" ", "\t", ")", "]", "'", '"')  # (`x.empty?`, `a?.b` and `A::B` are no ternary)
            if e.startswith(("||", "??"), i) or (e.startswith("//", i) and spaced):
                sep = e[i:i + 2]
            elif c == "?" and spaced and e[i + 1:i + 2] != ".":
                sep = "?:" if e[i + 1:i + 2] == ":" else "?"
            elif c == ":" and spaced and e[i + 1:i + 2] != ":" and "?" in seps:
                sep = ":"
        if sep:
            pieces.append("".join(cur))
            seps.append(sep)
            cur = []
            i += len(sep)
            continue
        cur.append(c)
        i += 1
    if not seps:
        return [e]
    pieces.append("".join(cur))
    keep = [p.strip() for p, s in zip(pieces, seps + [""]) if s != "?" and p.strip()]
    return keep[:_T_ALT_MAX - 1] + ["'\x01'"] if len(keep) > _T_ALT_MAX else keep or [e]


def _t_vals(expr, binds, depth=0):
    """The strings a text-language expression may be, when it starts with something the code spells out."""
    if depth < 6 and (alts := _t_alts(expr)) != [expr.strip()]:  # T-4686: `c ? a : b`, `a || b`, `(a)`: each branch
        out = []
        for a in alts:
            out += _t_cut(_t_vals(a, binds, depth + 1), 4)
        return _t_cut(out, 8)
    cands = [""]
    for i, t in enumerate(_terms(expr)):
        vs = _t_term(t, binds, depth)
        if not vs:
            if i == 0:
                return []
            vs = ["\x00"]
        cands = _t_cut([a + b for a in cands for b in _t_cut(vs, 4)], 8)
    return cands


def _t_term(t, binds, depth):
    if m := _T_QUOTED.match(t):
        quote, body = "'\"`"[m.lastindex - 1], m.group(m.lastindex)
        if quote == "'" or "$" not in body and "#{" not in body:
            return [body]
        cands, pos = [""], 0
        for x in _T_INTERP.finditer(body):
            cands = [c + body[pos:x.start()] for c in cands]
            name = next(g for g in x.groups() if g)
            if depth < 3:  # (a name holds at most five bindings, the unknown first when there were more: no slice)
                got = [v for b in binds.get(name, ()) for v in _t_cut(_t_vals(b, binds, depth + 1), 2)]
            else:
                got = ["\x01"] if name in binds else []  # names bound through more hops than are followed: unknown
            cands = _t_cut([c + v for c in cands for v in (got or ["\x00"])], 8)
            pos = x.end()
        return [c + body[pos:] for c in cands]
    if m := _T_JOIN.match(t):
        cands, paste = None, t.startswith("paste0")
        for a in _call_args(t, m.end(), True):  # every candidate of every part, cut to eight with the unknown last
            vs = _t_cut(_t_vals(a, binds, depth + 1) or ["\x00"], 4)
            cands = vs if cands is None else _t_cut(
                [c + v if paste else os.path.join(c, v) for c in cands for v in vs], 8)
        return [c for c in cands or () if not c.startswith("\x00")]
    if m := _T_NAME.match(t):
        if depth >= 3:
            return ["\x01"] if m.group(1) in binds else []
        return [v for b in binds.get(m.group(1), ()) for v in _t_cut(_t_vals(b, binds, depth + 1), 2)]
    return []


def _t_bindings(code):
    """name -> the expressions bound to it (first four; a fifth puts the unknown '\\x01' first, where every slice keeps
    it). The key "\\x01" is set when the code binds more than 400 times: what follows isn't read."""
    out = {}
    for n, m in enumerate(_T_BIND.finditer(code)):
        if n > 400:
            out["\x01"] = ["x"]
            break
        a = _call_args(code, m.end(), False)
        a = [_CUT] if a[-1:] == [_CUT] else a[:1]  # a value longer than the window: unknown, not its first part
        if a:
            got = out.setdefault(m.group(1), [])
            if len(got) < 4:
                got.append(a[0])
            elif got[0] != "'\x01'":
                got.insert(0, "'\x01'")
    return out


def _t_writes_mode(args, lang):
    """Whether the mode argument of an open call (second argument, or perl's) is a write."""
    if len(args) < 2:
        return False
    rest = ",".join(args[1:])
    if _TEXT_WRITE_FLAGS.search(rest):
        return True
    lits = [x.group(x.lastindex).split(":", 1)[0] for x in _T_QUOTED.finditer(rest)]  # ruby: "w:UTF-8"
    if lits:
        return any(_TEXT_WRITE_MODE.match(s) and set(s) & set("wxac+") for s in lits)
    return bool(re.match(r"[$@]?\w+$", args[1]))  # a mode held in a variable: taken as a write


_JS_FS = r"""['"](?:node:)?fs(?:/promises)?['"]"""
# (the name pattern starts at a word's first letter only: unanchored, `(\w+)\s*=` tried every letter of a long word and
# took quadratic time, T-4673 round 3)
_JS_FS_NAME = lazy(r"(?<![\w$])(\w+)\s*=\s*(?:await\s+)?require\(\s*" + _JS_FS + r"\s*\)"
                   r"|import\s+(?:\*\s+as\s+)?(\w+)\s+from\s+" + _JS_FS)
_JS_FS_PICK = lazy(r"\{([^{}]*)\}\s*(?:=\s*(?:await\s+)?require\(\s*|\bfrom\s+)" + _JS_FS)
_JS_FS_MEMBER = lazy(r"(?<![\w$])(\w+)\s*=\s*require\(\s*" + _JS_FS + r"\s*\)\s*\.\s*(\w+)")  # const w = require('fs').writeFileSync
_JS_PICKS_MAX = 400


def _js_fs_aliases(code):
    """T-4673 review: (the names that hold the fs module, {local name: the kind of fs call it is}, whether the code picks
    more names than are read) for node code that binds it: `const f = require('fs')`, `import * as f from 'fs'`, `const
    { writeFileSync: w } = require('fs')`, `import { writeFileSync as w } from 'fs'`, `const w = require('fs').writeFileSync`,
    `const { promises: p } = require('fs')`. A picked name counts as a write call whatever its spelling."""
    if "fs" not in code:
        return set(), {}, False
    recvs = {g for m in _JS_FS_NAME.finditer(code) for g in m.groups() if g}
    bare, many = {}, False
    for m in _JS_FS_MEMBER.finditer(code):
        if m.group(2) in _FS_NAMES:
            bare[m.group(1)] = _FS_NAMES[m.group(2)]
    for m in _JS_FS_PICK.finditer(code):
        parts = m.group(1).split(",")
        many = many or len(parts) > _JS_PICKS_MAX  # past this the rest isn't read: judged as unknown
        for part in parts[:_JS_PICKS_MAX]:
            bits = re.split(r"\s+as\s+|\s*:\s*", part.strip())
            if len(bits) in (1, 2) and re.fullmatch(r"\w+", bits[-1]):
                if bits[0] in _FS_NAMES:
                    bare[bits[-1]] = _FS_NAMES[bits[0]]
                elif bits[0] == "promises":
                    recvs.add(bits[-1])
    return recvs, bare, many


def _text_system_targets(code, langs):
    """T-4673: the targets of write calls in code of the languages in `langs` (the table above), as text."""
    out = []
    binds = _t_bindings(code)
    for lang in langs - {"tcl"}:
        rows = _TEXT_ROWS[lang]
        recvs, bare, many = _js_fs_aliases(code) if lang == "js" else (set(), {}, False)  # const f = require('fs'); { x as y }
        if many:  # more picked names than are read: a later one may be a write call
            out.append("\x01")
        names = {n for _, _, kinds in rows for n in kinds} | set(bare)
        word = re.compile(r"(?<![\w$@])(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")(?![\w$])")
        calls = 0
        for m in word.finditer(code):
            before = code[max(0, m.start() - 48):m.start()]
            r = re.search(r"(?:([A-Za-z_]\w*)|(\)))\s*(?:\.|::|->)\s*$", before)
            if re.search(r"(?:\.|::|->)\s*$", before) and not r:
                continue
            recv = (r.group(1) or r.group(2)) if r else None
            kind = next((kinds[m.group(1)] for rs, bare_ok, kinds in rows
                         if m.group(1) in kinds and ((recv is None and bare_ok) or recv in rs)), None)
            if kind is None and recv is None and m.group(1) in bare:
                kind = bare[m.group(1)]
            elif kind is None and recv in recvs:
                kind = _FS_NAMES.get(m.group(1))
            if kind is None:
                continue
            after = code[m.end():]
            if p := re.match(r"\s*\(", after):
                args = _call_args(code, m.end() + p.end(), True)
            elif (lang in _PAREN_LESS and re.match(r"[ \t]+[\"'$\w]", after)  # unlink "x"; File.delete "x"
                  or lang in _PAREN_LESS | {"lua"} and re.match(r"[\"']", after) and (code[m.start() - 1] if m.start() else " ") not in "'\""):
                args = _call_args(code, m.end(), False)  # unlink"x" (no space: perl, ruby; lua's f"x")
            else:
                continue
            calls += 1
            if calls > 300 or binds.get("\x01"):  # past the cap, or bindings the guard stopped reading: not ruled out
                out.append("\x01")
                return out
            if args and args[-1] == _CUT:  # a call longer than _call_args reads: what is past its window is unknown
                out.append("\x01")
            if lang == "r":
                named = [a.split("=", 1)[1] for a in args if re.match(r"\w+\s*=(?!=)", a)
                         and a.split("=", 1)[0].strip() in _R_PATH_ARGS]
                args = [a for a in args if not re.match(r"\w+\s*=(?!=)", a)] if kind != "open" else args
                out += [v for a in named for v in _t_vals(a, binds)]  # (at most eight, the unknown last: no slice)
                if kind == "named":
                    continue
            picks = {"first": args[:1], "second": args[1:2], "both": args[:2], "all": args}.get(kind, [])
            if kind == "open":
                if _t_writes_mode(args, lang):
                    picks = args[:1]
            elif kind == "plopen":
                picks = _perl_open_target(args, binds)
                out += picks
                continue
            if len(picks) > 8:  # a call with more targets than are read (File.delete a1, a2 … a9): the rest is unknown
                out.append("\x01")
            out += [v for a in picks[:8] for v in _t_cut(_t_vals(a, binds), 3)]
            if len(out) > 256:
                out.append("\x01")
                return out
    if "tcl" in langs:
        out += _tcl_system_targets(code)
    return out


def _perl_open_target(args, binds):
    """perl's open: 2-argument (">path", the mode and path in one string) or 3-argument (mode, then path), writes only."""
    if len(args) < 2:
        return []
    modes = _t_vals(args[1], binds)  # (at most eight, the unknown "\x01" last: it is a target of its own, no slice)
    out = []
    for v in modes:
        if "\x01" in v:
            out.append(v)
            continue
        m = re.match(r"\s*(\+?>>?|\+<)\s*(.*)$", v, re.S)
        if not m:
            continue
        rest = m.group(2).strip()
        if rest and not (rest.startswith(":") and len(args) > 2):  # ">:encoding(UTF-8)", path: a layer, not the path
            out.append(rest)
        elif len(args) > 2:
            out += _t_vals(args[2], binds)
    return out


def _tcl_system_targets(code):
    """T-4673: the write targets of Tcl's open (a write mode) and file delete/rename/mkdir/copy/link."""
    sets = {m.group(1): m.group(2).strip('{}"') for m in _TCL_SET.finditer(code)}

    def word(w):
        w = w.strip().strip('{}"')
        return sets.get(w[1:], "\x00") if w.startswith("$") else w
    out = []
    for m in _TCL_OPEN.finditer(code):
        mode = (m.group(2) or "").strip('{}"')
        if re.match(r"^(?:[wa][+bt]*|r\+[bt]*)$", mode) or re.search(r"WRONLY|RDWR|APPEND|CREAT|TRUNC", mode):
            if not m.group(1).strip('{}"').startswith("|"):
                out.append(word(m.group(1)))
    for m in _TCL_FILE.finditer(code):
        words = [w for w in m.group(2).split() if not w.startswith("-")]
        pick = {"copy": words[-1:], "link": words[:1], "attributes": words[:1]}.get(m.group(1), words)
        out += [word(w) for w in pick]
    return out


# T-4686: the shell strings node, perl, ruby, php, lua, R, julia and tcl code starts (what _spawned_commands does for
# python), read as commands by check_bash: `execSync('tee /etc/x')` is `tee /etc/x`, a list `spawnSync('sh', ['-c', S])`
# is `sh -c S`, a part the code computes is "$_" (an unknown). The call is found by name (the table), its arguments
# read as the write scan reads them (_call_args, _t_vals: literals, names bound once, concatenations, each branch of
# a ternary); a command the code builds from something it doesn't spell out is left to the shell's own judgement.
_SPAWN_WORDS = ("exec", "spawn", "system", "popen", "passthru", "execute", "`", "%x", "qx", "capture", "pipeline",
                "readpipe", "shell", "open", "Bun.")
_SPAWN_RX = {
    "js": lazy(r"(?<![\w$])(?:execSync|execFileSync|execFile|exec|spawnSync|spawn)\s*\("),
    "pl": lazy(r"(?<![\w$@%>])(?:system|exec|readpipe)(?![\w$])"),
    "rb": lazy(r"(?<![\w$@])(?:system|exec|spawn|popen|capture2e?|capture3|popen2e?|popen3)(?![\w$?!])"),
    "php": lazy(r"(?<![\w$>])(?:system|exec|shell_exec|passthru|popen|proc_open|pcntl_exec)\s*\("),
    "lua": lazy(r"(?<![\w.])(?:os\.execute|io\.popen)(?![\w$])"),
    "r": lazy(r"(?<![\w.])(?:system2?|shell)\s*\("),
    "tcl": lazy(r"(?<![\w$])exec[ \t]+")}
_SPAWN_TICKS = lazy(r"`(?:[^`\\]|\\.)*`")
_SPAWN_QX = lazy(r"""(?<![\w$@%>])(?:qx\s*([({\[</|!'"])|qx(\#)|%x([({\[</|!'"\#]))""")
_SPAWN_PIPE_OPEN = lazy(r"(?<![\w$@%>])open\s*\(")
_SPAWN_CWD = lazy(r"\b(?:cwd|chdir)\s*(?::|=>)\s*([^,}\n]+)")
_SPAWN_CAP = 48
_SPAWN_CLOSE = {"(": ")", "{": "}", "[": "]", "<": ">"}


def _spawn_words(e, binds):
    """The words an array expression holds ([a, b], c("a"), array(), qw(a b)) as lists of candidate strings, or None
    when `e` isn't one."""
    e = e.strip()
    if m := re.match(r"qw\s*([(\[{/<])(.*)[)\]}/>]$", e, re.S):
        return [[w] for w in m.group(2).split()]
    if m := re.match(r"(?:\[|(?:c|array|list|vector)\s*\(|\barray\s*\()", e):
        return [_t_cut(_t_vals(a, binds) or ["\x00"], 4) for a in _call_args(e, m.end(), True)]
    return None


def _spawn_shell(cands):
    """Shell text from the candidate strings of a command (an unknown part: "$_")."""
    return re.sub(r"[\x00\x01]+", "$_", cands)


_SPAWN_UNK = ("system", "interpreter code starts a shell command the guard can't read (the command is computed, not "
              "spelled out): write the command out")


def _spawn_texts(args, binds, lang, name):
    """(the command texts one spawn call may start, marks) from its (comma-separated) argument expressions. `marks` holds
    "big" when a cut dropped values or combinations (the unknown), "unk" when the whole command is one the code doesn't
    spell out (it starts with a computed part, or with nothing the scan can read)."""
    marks = set()
    args = [a for a in args if a != _CUT]
    if lang == "rb" and args and args[0].startswith("{"):  # system({"ENV" => "1"}, "rm", …): the first argument is env
        args = args[1:]
    if not args:
        return [], marks
    words = _spawn_words(args[0], binds)
    if words is None and lang == "js" and name != "exec" and name != "execSync" and len(args) > 1:
        tail = _spawn_words(args[1], binds)  # spawnSync('rm', ['/etc/x']): the program, then its argument list
        words = [_t_cut(_t_vals(args[0], binds) or ["\x00"], 4)] + tail if tail is not None else None
    if words is None and lang == "r" and name == "system2":
        tail = [a.split("=", 1)[1] if re.match(r"args\s*=(?!=)", a) else a for a in args[1:2] + [x for x in args[2:] if re.match(r"args\s*=", x)]]
        tail = _spawn_words(tail[0], binds) if tail else None
        words = [_t_cut(_t_vals(args[0], binds) or ["\x00"], 4)] + (tail or [])
    if words is None and lang in ("pl", "rb") and len(args) > 1 and name != "readpipe":  # several words: no shell
        rest = [a for a in args if not re.match(r"\{|:?\w+\s*(?:=>|:(?!:))", a)]
        words = [_t_cut(_t_vals(a, binds) or ["\x00"], 4) for a in rest]
    if words is not None:  # a program and its arguments, quoted: what the shell sees as separate words
        texts = [[]]
        for vs in words:
            if any("\x01" in v for v in vs):  # a cut left the unknown last
                marks.add("big")
            texts = [t + [v] for t in texts for v in vs]
            if len(texts) > 16:  # combinations past the sixteenth are dropped: not read, so unknown
                marks.add("big")
                texts = texts[:16]
        return [" ".join('"$_"'.join(shlex.quote(p) for p in re.split(r"[\x00\x01]+", w)) for w in t) for t in texts], marks
    vals = _t_vals(args[0], binds)
    if not vals or any(re.match(r"\s*[\x00\x01]", v) for v in vals):  # the command itself is computed
        marks.add("unk")
    if any("\x01" in v for v in vals):
        marks.add("big")
    return [_spawn_shell(v) for v in _t_cut(vals or ["\x00"], 8)], marks


_JS_CP = r"""['"](?:node:)?child_process['"]"""
_JS_FNS = "execSync|execFileSync|execFile|exec|spawnSync|spawn"
_JS_DESTR = lazy(r"\{([^{}]*)\}\s*=\s*require\s*\(\s*" + _JS_CP + r"\s*\)|\bimport\s*\{([^{}]*)\}\s*from\s*" + _JS_CP)
_JS_FN_ALIAS = lazy(r"\b(?:const|let|var)\s+([\w$]+)\s*=\s*(?:(?:require\s*\(\s*" + _JS_CP + r"\s*\)|[\w$]+)\s*\.\s*)?("
                    + _JS_FNS + r")\s*(?=[;,\n)]|$)")


def _js_aliases(code):
    """{alias: the child_process function it is} for `const {execSync: x} = require('child_process')`, `import {exec as
    x} from 'child_process'` and `const x = cp.execSync` (T-4686 review: a call through the alias read as no spawn)."""
    out = {}
    for m in _JS_DESTR.finditer(code):
        for part in (m.group(1) or m.group(2) or "").split(","):
            if (p := re.fullmatch(r"\s*(" + _JS_FNS + r")\s*(?::|\bas\b)\s*([\w$]+)\s*", part)):
                out[p.group(2)] = p.group(1)
    for m in _JS_FN_ALIAS.finditer(code):
        out[m.group(1)] = m.group(2)
    return {a: n for a, n in out.items() if a != n}


def _spawn_body(code, start, opener):
    """(the body of a qx{…} or %x(…) that opens before `start`, whether its close was found): brackets nest, a backslash
    skips a character, the same character closes a quote-like opener. The search is bounded."""
    close = _SPAWN_CLOSE.get(opener, opener)
    depth, i, stop = 1, start, min(len(code), start + 3000)
    while i < stop:
        c = code[i]
        if c == "\\":
            i += 2
            continue
        if opener != close and c == opener:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return code[start:i], True
        i += 1
    return code[start:stop], False


def _spawn_in(code, lang):
    """(command texts, marks) for the spawn calls of one language in `code`: marks is the set of "big" (a cap or a cut
    left something unread) and "unk" (a command the code doesn't spell out)."""
    binds, out, marks = _t_bindings(code), [], set()

    def add(texts, cwd="", mk=()):
        marks.update(mk)
        for t in texts:
            if len(out) >= _SPAWN_CAP:
                marks.add("big")
                return
            out.append(cwd + t)

    def put(res, cwd=""):
        add(res[0], cwd, res[1])
    if lang in ("pl", "rb", "php", "jl"):  # `cmd` runs it (a template string in node: no)
        for m in _SPAWN_TICKS.finditer(code):
            vals = _t_vals(m.group(0), binds)
            add((_spawn_shell(v) for v in _t_cut(vals or ["\x00"], 4)),
                mk=["unk"] if not vals or any(re.match(r"\s*[\x00\x01]", v) for v in vals) else ())
    if lang in ("pl", "rb"):  # qx{…}, %x(…)
        for m in _SPAWN_QX.finditer(code):
            body, closed = _spawn_body(code, m.end(), next(g for g in m.groups() if g))
            add([_spawn_shell(body)], mk=([] if closed else ["big"]) + (["unk"] if re.match(r"\s*(?:[$@]|#\{)", body) else []))
    if lang == "js" and "child_process" not in code and not re.search(r"\bBun\.spawn|\bDeno\.run", code):
        return out, marks  # (exec and spawn are RegExp and other methods too: only where the code has the module)
    if lang == "js" and re.search(r"\bBun\.spawn(?:Sync)?\s*\(\s*\[", code):
        for m in re.finditer(r"\bBun\.spawn(?:Sync)?\s*\(", code):
            put(_spawn_texts(_call_args(code, m.end(), True)[:1], binds, "js", "spawn"))
    if lang == "pl":
        for m in _SPAWN_PIPE_OPEN.finditer(code):  # open(F, "cmd |"), open(F, "| cmd"), open(F, "-|", "cmd", args)
            args = _call_args(code, m.end(), True)
            if len(args) > 1:
                for v in _t_cut(_t_vals(args[1], binds), 4):
                    s = v.strip()
                    if s in ("-|", "|-") and len(args) > 2:
                        put(_spawn_texts(args[2:], binds, "pl", "system"))
                    elif s.startswith("|") or s.endswith("|") and not s.startswith((">", "<")):
                        add([_spawn_shell(s.strip("|"))])
    rx = _SPAWN_RX.get(lang)
    calls = [(m, None) for m in rx.finditer(code)] if rx else []
    if lang == "js" and (aliases := _js_aliases(code)):  # x('rm …') where x is execSync under another name
        calls += [(m, aliases) for m in re.finditer(r"(?<![\w$.])(" + "|".join(map(re.escape, aliases)) + r")\s*\(", code)]
    for m, aliases in calls:
        after = code[m.end():]
        name = re.sub(r"^.*\.", "", m.group(0).strip().rstrip("(").strip())
        if aliases:
            name = aliases[m.group(1)]
        if m.group(0).rstrip().endswith("("):
            args = _call_args(code, m.end(), True)
        elif p := re.match(r"\s*\(", after):
            args = _call_args(code, m.end() + p.end(), True)
        elif lang in ("pl", "rb", "lua", "tcl") and re.match(r"[ \t]*[\"'$\w]", after):
            args = _call_args(code, m.end(), False)
        else:
            continue
        if lang == "tcl":  # exec rm /etc/x: the words are the command (a $name left as one)
            add([_spawn_shell(" ".join(a.strip('{}"') for a in re.findall(r'\{[^}]*\}|"[^"]*"|\S+', after.split("\n")[0].split(";")[0])))])
            continue
        cwd = ""
        if lang in ("js", "rb") and (c := _SPAWN_CWD.search(" ".join(args[1:]))):
            cvs = _t_vals(c.group(1), binds)
            cwd = "cd " + (shlex.quote(cvs[0]) if len(cvs) == 1 and not re.search(r"[\x00\x01]", cvs[0]) else '"$_"') + " && "
        put(_spawn_texts(args, binds, lang, name), cwd)
    return out, marks


_JS_ARGV_AT = lazy(r"(?<![\w$.])process\s*\.\s*argv\s*\[\s*(\d+)\s*\]")
_JS_ARGV_SLICE = lazy(r"(?<![\w$.])process\s*\.\s*argv\s*\.\s*slice\s*\(\s*(\d+)\s*\)")


def _node_argv(cmd):
    """T-4736: the words after the code of the one `node -e` / `node -p` in cmd (process.argv[1:]), or None when there
    isn't exactly one such command, it takes words from elsewhere (xargs), or the first word is one node would read as
    its own option."""
    found = []
    for c in _split(_tokens(_lines(_strip_heredocs(cmd)))):
        argv, xargs = _strip_wrappers(c.argv)
        if argv and re.match(r"^(?:node|nodejs)$", os.path.basename(argv[0])):
            if len(argv) > 2 and argv[1] in ("-e", "-p", "--eval", "--print") and not xargs:
                found.append(argv[3:])
            else:
                found.append(None)
    if len(found) != 1 or not found[0] or found[0][0].startswith("-"):
        return None
    return found[0]


def _js_argv(code, words):
    """code with the process.argv[N] and process.argv.slice(N) it spells out (N ≥ 1) replaced by the literal words the
    outer command line holds (T-4736); one a word the shell may expand, or past the end, stays as it is (an unknown)."""
    def put(w):
        return json.dumps(w, ensure_ascii=False)

    def one(m):
        i = int(m.group(1))
        return put(words[i - 1]) if 1 <= i <= len(words) and _argv_clean([words[i - 1]]) else m.group(0)

    def many(m):
        i = int(m.group(1))
        ws = words[i - 1:] if i >= 1 else []
        return "[" + ", ".join(put(w) for w in ws) + "]" if _argv_clean(ws) else m.group(0)
    return _JS_ARGV_SLICE.sub(many, _JS_ARGV_AT.sub(one, code))


def _text_spawned(cmd, ctx, dry):
    """T-4686: (command texts, marks) for the shell strings the node, perl, ruby, php, lua, R, julia and tcl code in a
    command starts; marks holds "big" and "unk" as _spawn_in gives them. The cheap gates come first: an interpreter
    other than python and one word of a spawn call."""
    if not _INTERP.search(dry) or not any(w in cmd for w in _SPAWN_WORDS):
        return [], set()
    names = [n for m in _INTERP.finditer(_mask_fm(dry, ctx))
             if (n := re.sub(r"[0-9.]+$", "", os.path.basename(m.group(1)))) in _LANG_OF]
    langs = {_LANG_OF[n] for n in names}
    if not langs:
        return [], set()
    js = [n for n in names if _LANG_OF[n] == "js"]  # T-4736 review: node's words are node's alone, never another js
    node_only = len(js) == 1 and js[0] in ("node", "nodejs")  # interpreter's process.argv (bun, deno, a second node)
    walk = _interp_walk(cmd, ctx)
    units = ([(t, ls) for t, ls, _ in walk[0]] if walk is not None
             else [(_interp_code(cmd), langs)])
    out, marks = [], set()
    words = _node_argv(cmd) if node_only and "process" in cmd and "argv" in cmd else None
    for text, ls in units:
        text = text.replace('\\"', '"').replace("\\$", "$")
        if words and "argv" in text and "js" in ls:
            text = _js_argv(text, words)
        for lang in ls - {"py"}:
            got, mk = _spawn_in(text, lang)
            out += got
            marks |= mk
            if len(out) > _SPAWN_CAP:
                return out[:_SPAWN_CAP], marks | {"big"}
    return list(dict.fromkeys(out)), marks


_RUN_NAME = lazy(rf"(?:{_INTERP_NAMES}|R)")
# T-4673 review: the files a heredoc may be saved into without ever being run (text, config, markup)
_DATA_EXT = (".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv", ".html", ".css", ".log", ".cfg", ".conf",
             ".ini", ".toml", ".xml", ".rst", ".svg", ".diff", ".patch")
_RUN_FLAGS = {"py": "c", "js": "ep", "pl": "eE", "rb": "e", "php": "r", "lua": "e", "r": "e", "jl": "eE"}  # code-word flags
_FLOW_WORDS = _KEYWORDS | {"for", "case", "select", "esac", "fi", "done", "function"}
_WALK_UNITS_MAX = 200
_TOO_BIG = ("system", "interpreter code too large to read whole (more writes, names or values than the guard follows): "
            "split it or name the paths plainly")
_UNBOUND = ("system", "interpreter code writes through a function parameter the guard can't bind to a value (the function "
            "is handed on to a callback, called through *args or **kwargs, or mapped over a list the code doesn't spell "
            "out): call it with literal paths, or map it over a list written in the code")


def _wrapper_dir(prefix):
    """T-4673 round 3: the folder word a wrapper before a command sets (env -C DIR, env --chdir DIR or --chdir=DIR, sudo -D
    DIR), None when there is none. `prefix` is the words _strip_wrappers dropped."""
    for i, a in enumerate(prefix):
        base = os.path.basename(a)
        if base not in ("env", "sudo", "doas"):
            continue
        short, valued = ("-C", ("-u", "--unset")) if base == "env" else ("-D", ("-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U"))
        j = i + 1
        while j < len(prefix) and (prefix[j].startswith("-") or "=" in prefix[j]):
            w = prefix[j]
            if w.startswith("--chdir="):
                return w[8:]
            if w in ("--chdir", short):
                return prefix[j + 1] if j + 1 < len(prefix) else None
            j += 2 if w in valued else 1
    return None


def _cd_union(text, ctx):
    """T-4673: every folder a cd in text may leave the shell in, for the places the walk below can't follow (and the
    fallback): where it starts, each existing folder a cd or pushd names (quotes and -L/-P skipped), and where a climbing
    cd through a link goes."""
    out = [ctx.cwd]
    for m in _CD_TARGET.finditer(text):
        w = m.group(2)
        if not _unresolvable(w) and os.path.isdir(d := _resolve(_expand(w, ctx), ctx.cwd)):
            out.append(d)
    return list(dict.fromkeys(out + _climb_dirs(text, ctx)))


def _run_inline(lang, args):
    """Whether an interpreter's own arguments carry its code (-c, -e, -r, --eval, deno eval): no script file to read."""
    flags, valued, i = _RUN_FLAGS.get(lang, ""), _OPT_VALUE.get(lang, ""), 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            return False
        if a.startswith("--"):
            if lang == "js" and a in ("--eval", "--print"):
                return True
            i += a in _LONG_VALUE
            continue
        if a.startswith("-") and len(a) > 1:
            for j, f in enumerate(a[1:]):
                if f in flags:
                    return True
                if f in valued:
                    i += j == len(a) - 2
                    break
            continue
        return lang == "js" and a == "eval"  # the first plain word is the script (or the module of -m)
    return False


def _feed_is_data(c, name, argv, onward):
    """Whether what a command reads on stdin (a heredoc, a here-string) only becomes text: it prints it, keeps it, or
    saves it to a data file, and no pipe carries it on. A file of another name may be run later, so that is code."""
    if onward or c.depth > 0 or name in ("dd", "cp", "install", "ln", "mv", "rsync"):  # a group may feed a process substitution
        return False
    targets = [r for r in c.redirs if not r.startswith("/dev/")]
    if name in ("tee", "sponge"):
        targets += [a for a in argv[1:] if not a.startswith("-")]
    return all(t.lower().endswith(_DATA_EXT) for t in targets)


def _interp_walk(cmd, ctx):
    """T-4673 review: (units, places) read the way the shell runs the line, or None when the line has a form this does
    not follow (the caller reads the whole line instead). A unit is (code, languages, folders): code an interpreter is
    handed (its arguments, a heredoc or here-string it reads, and, for one that runs a script file or its stdin, the
    text the line writes to files or into a pipe) with the folders the shell may be in where it runs. Text that merely
    sits in the line (a commit message, a heredoc saved to a .md file) is no unit. The folders follow the cd, pushd and
    popd commands in order: a straight cd into an existing folder replaces them, a subshell's cd ends with it, and a cd
    that may not happen (after && or ||, in a pipe, a loop or group, in the background) adds to them."""
    lines, bodies = _heredoc_split(cmd)
    text = "\n".join(lines)
    if "$'" in text or len(re.findall(r"(?<!<)<<(?!<)", text)) != len(re.findall(_HEREDOC_START, text)):
        return None  # $'…' quoting, or a heredoc form the split doesn't know
    if ("`" in text or "$(" in text) and (bodies or re.search(r"(?:^|[\s;&|(])(?:cd|pushd|popd)(?=\s|$|[;&|)])", text)):
        return None  # a command inside a substitution: its heredocs and its cd are not placed
    if _shlex_fails(_lines(text)):
        return None  # quotes shlex can't pair: the words below would be a guess
    cmds = _split(_tokens(_lines(text)))
    if len(cmds) > 2000 or any(c.depth < 0 for c in cmds) or sum(op == "<<" for c in cmds for op, _ in c.feeds) != len(bodies):
        return None
    vague = (any(c.argv and c.argv[0] in _FLOW_WORDS for c in cmds) or bool(re.search(r"\)\s*\{", text))  # a loop, branch,
             or "CDPATH" in text or bool(os.environ.get("CDPATH")))  # group or function: a cd there may not happen
    memo, here, scopes, nh, steps = {}, [ctx.cwd], [], 0, []  # steps: (command, argv, name, folders, fed texts, onward)

    def anywhere():
        if "u" not in memo:
            memo["u"] = _cd_union(text, ctx)
        return memo["u"]

    def join(a, b):
        return list(dict.fromkeys(a + b))
    for k, c in enumerate(cmds):
        fed = []
        for op, w in c.feeds:
            if op == "<<":
                fed.append("\n".join(bodies[nh]))
                nh += 1
            else:
                fed.append(w)
        while scopes and c.depth < scopes[-1][0]:
            here = scopes.pop()[1]  # a subshell ended: its cd goes with it
        top = scopes[-1][0] if scopes else 0
        while top < c.depth:
            top += 1
            scopes.append((top, list(here)))
        prefix = []  # (the words dropped, an env -S string split into its own: its --chdir DIR is seen)
        argv, via_xargs = _strip_wrappers(c.argv, prefix)
        if not argv:
            continue
        name = os.path.basename(argv[0])
        nxt = cmds[k + 1].op if k + 1 < len(cmds) else ""
        onward = nxt in ("|", "|&")
        piped = c.op in ("|", "|&") or onward
        if name in _CDS:
            odd = any(not (p in _KEYWORDS or "=" in p or os.path.basename(p) in ("builtin", "command")) for p in prefix)
            rest = argv[1:]
            while rest and rest[0] in ("-L", "-P", "-e", "-@", "--"):
                rest = rest[1:]
            if (name == "popd" or rest[:1] == ["-"] or len(rest) > 1 or vague
                    or name == "pushd" and (not rest or rest[0].startswith(("+", "-")))
                    or _unliteral(rest[0] if rest else ctx.home) or re.search(r"[*?\[]", rest[0] if rest else "")):
                here = join(here, anywhere())  # a form whose place the guard can't name: any folder the line names
                continue
            new = _moved(here, [], rest[0] if rest else ctx.home, True, False, ctx)[0]
            sure = not (piped or odd) and c.op not in ("||", "&&") and nxt != "&" and all(
                os.path.isdir(x) and os.access(x, os.X_OK) for x in new)
            here = new if sure else join(here, new)
            continue
        if (name in ("eval", "source", ".", "trap", "mapfile", "readarray", "exec") or _unresolvable(argv[0])) and not piped:
            here = join(here, anywhere())  # code or a file the guard can't read may cd
        folders = list(here)
        if (w := _wrapper_dir(prefix)) is not None:  # env -C DIR, sudo -D DIR: it runs there
            folders = join(here, anywhere()) if _unliteral(w) or re.search(r"[*?\[]", w) else _moved(here, [], w, True, False, ctx)[0]
        steps.append((c, argv, name, folders, fed, onward, via_xargs))
    runs = [s for s in steps if _RUN_NAME.fullmatch(s[2])]
    if len(runs) < len(_INTERP.findall(_mask_fm(text, ctx))):
        return None  # an interpreter the walk didn't see as a command word (under a wrapper it doesn't know, in a string)
    units, loose, places = [], [], []
    if (bodies or any(s[4] for s in steps)) and any(_RUNS_STDIN.fullmatch(s[2]) or _unresolvable(s[1][0]) for s in steps):
        return None  # a shell may read a heredoc or here-string as commands (its code is no interpreter's): read whole
    for c, argv, name, folders, fed, _, via_xargs in runs:
        lang = _LANG_OF.get(re.sub(r"[0-9.]+$", "", name), "py")
        places = join(places, folders)
        inline = _run_inline(lang, argv[1:])
        if via_xargs or not inline:  # xargs hands it what the pipe carries (its code, whatever its flags)
            loose.append((lang, folders))
        # an inline run's every argument may be its code, whatever it looks like (unlink"x" has no space or paren)
        units += [(a, {lang}, folders) for a in argv[1:] if inline or re.search(r"[\s(]", a)]
        units += [(f, {lang}, folders) for f in fed]
    if loose:  # a script file or stdin: the text this line saves or pipes may be that code
        langs, folders = {lang for lang, _ in loose}, []
        for _, fs in loose:
            folders = join(folders, fs)
        for c, argv, name, _, fed, onward, _ in steps:
            if _RUN_NAME.fullmatch(name) or _feed_is_data(c, name, argv, onward):
                continue
            units += [(a, langs, folders) for a in argv[1:] if re.search(r"[\s(]", a)]
            units += [(f, langs, folders) for f in fed]
    if len(units) > _WALK_UNITS_MAX:
        return None
    return units, places or [ctx.cwd]


def _unit_targets(code, langs):
    """The write targets (as text) of one unit of interpreter code: python through its parse tree (text rows when it
    does not parse), the other languages through the table."""
    cands, text_langs = [], set(langs) - {"py"}
    if "py" in langs:
        try:
            cands += _py_system_targets(code)
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            text_langs.add("py")  # not python as written: the text table reads it
    if text_langs:
        cands += _text_system_targets(code.replace('\\"', '"').replace("\\$", "$"), text_langs)
    return cands


_CHDIR = lazy(r"(?<![\w$@])(?:f?chdir|setwd)\b")  # os.chdir, process.chdir, Dir.chdir, perl's chdir, php's, R's setwd
_CHDIR_CD = lazy(r"(?<![\w$@.])cd\b")  # tcl's and julia's cd (a word of every shell script: only read in those)
_CHDIR_UNK = ("system", "interpreter code changes its folder where the guard can't tell to (chdir) and then writes a "
              "relative path: write the path in full")
_CHDIR_MAX = 16


def _chdir_bases(code, langs, folders, ctx):
    """T-4673 round 3: (folders, unknown) for one unit of interpreter code: `folders` (where the shell may be when it
    starts) widened by each place a chdir in the code may leave it, the literal targets followed in order (the code may
    not reach one, so they add), and `unknown` when a target is not a literal (an argument, a computed name): a relative
    write after it lands where the guard can't tell. Code with no chdir word pays one pattern search."""
    ms = list(_CHDIR.finditer(code))
    if langs & {"tcl", "jl"}:
        ms = sorted(ms + list(_CHDIR_CD.finditer(code)), key=lambda m: m.start())
    if not ms:
        return folders, False
    binds, dirs, unknown = _t_bindings(code), list(folders), bool(len(ms) > _CHDIR_MAX)
    for m in ms[:_CHDIR_MAX]:
        after = code[m.end():m.end() + 40]
        if p := re.match(r"\s*\(", after):
            args = _call_args(code, m.end() + p.end(), True)
        elif re.match(r"[ \t]+[\"'$\w]", after):
            args = _call_args(code, m.end(), False)
        else:
            continue  # the word without a call: a name in a string or a comment
        vals = _t_vals(args[0], binds) if args else []
        if not vals or any("\x00" in v or "\x01" in v for v in vals):
            unknown = True
            continue
        for v in vals:
            dirs = _moved(dirs, [], v, False, False, ctx)[0][:64]
    return dirs, unknown


def _judge_system(cands, bases, ctx, out, rel_unknown=False):
    """Add to `out` the system hits of the write targets `cands`: an absolute path as it is, a relative one in each of
    `bases` (a list, or a function that gives one when first needed; a relative path is a hit of its own when
    `rel_unknown`: the code changed its folder to one the guard can't name). The cap marker is a hit of its own."""
    got = None
    for c in dict.fromkeys(cands):
        if not c:
            continue
        if "\x01" in c:
            why = _UNBOUND if "\x01\x01" in c else _TOO_BIG  # T-4736: an unbound parameter says so
            if why not in out:
                out.append(why)
            continue
        if c.startswith("\x00"):
            continue
        t = _expand(c.replace("\x00", "_"), ctx)
        if not os.path.isabs(t) and rel_unknown and _CHDIR_UNK not in out:
            out.append(_CHDIR_UNK)
        if not os.path.isabs(t) and got is None:
            got = bases() if callable(bases) else bases
        for path, real in ([_walk(t, "/")] if os.path.isabs(t) else [_walk(t, b) for b in got]):
            out += [h for h in _judge(classify_write, path, real, ctx, " (written from interpreter code)")
                    if h[0] == "system" and h not in out]


def _interp_system_writes(cmd, ctx):
    """T-4673: [("system", why)] for each system path interpreter code writes (see the block above). Commands whose code
    has no write-call word pay one pattern search. The code is read as the shell runs it (_interp_walk); a line that
    walk can't follow is read whole, from every folder a cd in it names."""
    code = _interp_code(cmd)
    if not _SYS_GATE.search(code):
        return []
    out = []
    if (walk := _interp_walk(cmd, ctx)) is not None:
        for text, langs, folders in walk[0]:
            folders, unknown = _chdir_bases(text, langs, folders, ctx)  # (a chdir inside the code moves its relative writes)
            _judge_system(_unit_targets(text, langs), folders, ctx, out, unknown)
        return out
    langs, py = set(), False
    for m in _INTERP.finditer(_mask_fm(_drop_data_heredocs(cmd), ctx)):
        name = re.sub(r"[0-9.]+$", "", os.path.basename(m.group(1)))
        if name in _LANG_OF:
            langs.add(_LANG_OF[name])
        else:
            py = True
    cands = []
    if py:
        for src in _python_sources(cmd):
            cands += _unit_targets(src, {"py"})
    if langs:
        cands += _text_system_targets(code.replace('\\"', '"').replace("\\$", "$"), langs)
    folders, unknown = _chdir_bases(code, langs | ({"py"} if py else set()), _cd_union(cmd, ctx), ctx) if cands else ([], False)
    _judge_system(cands, folders, ctx, out, unknown)
    return out


def _interpreter_writes(cmd, ctx):
    """Interpreter code (heredoc, -c, -e) that writes files: every quoted path it names counts as a write target.

    Coarse on purpose: a script that names a protected path and writes anything is treated as writing it. Code that
    imports Sampo's modules and calls their writers bypasses fm (the only state writer): that needs core, i.e. the
    user's yes, rather than never-authorizable state-direct, because the text match can't tell code from test data."""
    if not _INTERP.search(_mask_fm(_drop_data_heredocs(cmd), ctx)):
        return []
    state = ctx.state_dir or (os.path.join(ctx.foreman_home, "state") if ctx.foreman_home else None)
    if why := _drives_fm(_drop_data_heredocs(cmd, only=lambda line: _writes_unrun_source(line, cmd, ctx)
                                             or _writes_unrun_draft(line, cmd, ctx)), ctx.cwd,
                         cache=os.path.join(state, "ward-index") if state and os.path.isdir(state) else None):
        # T-0740: 9 repeats in a day; T-0934: `cd DIR; python3 - <<EOF` is refused only because of the `;`. T-1171: 12
        # of 31 refusals in four days wrote a test with cat <<EOF and ran the tests in the same command
        return [("core", f"interpreter code driving Sampo's modules: {why} (use the fm CLI; to edit a Sampo source file "
                         "whose text names its modules, use the Edit or Write tool"
                         + ("; chain the cd with && instead of ;, or pass the path" if re.search(r"\bcd\s+[^\n;&|]+;", cmd)
                            else "")
                         + ("; write the file in one command and run it in the next: the write alone is data"
                            if re.search(r"\b(?:cat\s*>>?|tee\b)[^\n]*<<", cmd) else "") + ")")]
    code = _interp_code(cmd)
    # T-0150/T-0155: a backtick runs code unless every interpreter here treats it as text (markdown in a Python heredoc)
    ticks = "`" in code and any(not _TEXT_TICKS.match(m.group(1)) for m in _INTERP.finditer(cmd))
    if _CLAUDE_IN_CODE.search(code) and (_EXEC_API.search(code) or ticks):
        return [("plugin", "interpreter code running claude's plugin, MCP or config commands" + plugin_mark("?"))]
    proved = _proved_writes(cmd, ctx)  # T-0176: a script that provably writes only its open() files
    system = _interp_system_writes(cmd, ctx)  # T-4673: the system paths it writes (the filter below keeps the guarded ones)
    if proved is None and not _WRITE_API.search(cmd) and not _OPENER.search(cmd) \
            and not (ctx.unbriefed and _computed_path_write(cmd)):
        return system  # T-4724: a computed path opened with a mode the regex can't read is a write the guard can't rule out
    found = []
    if proved is None:  # T-1458: after a provable `cd DIR &&` chain a relative name opens there, not in ctx.cwd
        units, _, starters, lead = _python_units(cmd)
        only = _chain_dir(starters, "\n".join(lead), ctx) if units else None
        bases = [only or ctx.cwd]
        if not only and re.search(r"(?:^|[\s;&|(])(?:cd|pushd)\s", cmd):
            # T-4673: -c code (and code the chain can't pin to one folder) opens a relative name where the shell may be
            # when it runs: the places the walk follows in order, else after any cd the line names
            walk = _interp_walk(cmd, ctx)
            bases = list(dict.fromkeys(bases + (walk[1] if walk is not None else _cd_union(cmd, ctx))))
    for path, real in proved if proved is not None else [
            _walk(_expand(x, ctx), b) for x in _quoted_paths(cmd) for b in bases]:
        what = f"{path} (written from interpreter code)" if "$" not in path else (  # T-4138: a $name the shell fills in
            "a file the script writes at a path the shell fills in, which the guard can't read (written from interpreter code)")
        hits = [(cat, what) for cat in classify_write(path, ctx)]
        if real:  # T-4667: a '..' after a link: where it really goes counts too (named by that path, no yes covers it)
            hits += [(cat, f"{real} (a '..' after a link in {path}) (written from interpreter code)")
                     for cat in classify_write(real, ctx) if cat not in {c for c, _ in hits}]
        found += [h for h in hits if h[0] in _GUARDED_BY_PATH and h not in found]
    if proved is None and not found and ctx.unbriefed and _computed_path_write(cmd):
        # T-4138: no task is active and the file is named by text the script builds: not a path the scan can name
        found = [("brief", "a file the script writes at a computed path the guard can't read (written from interpreter code)")]
    return found + [h for h in system if h not in found]


_PATH_LITERAL = lazy(r"""(?:[~/.]|[\w.-]+/)[^'"\s]*""")  # what _QUOTED takes between its quotes


def _is_sys_path_call(f):
    import ast
    return (isinstance(f, ast.Attribute) and f.attr in ("insert", "append") and isinstance(f.value, ast.Attribute)
            and f.value.attr == "path" and isinstance(f.value.value, ast.Name) and f.value.value.id == "sys")


# T-4138: str methods whose literal arguments are text to test or cut, never a file the call touches
_STR_METHODS = frozenset("startswith endswith split rsplit strip lstrip rstrip removeprefix removesuffix partition "
                         "rpartition find rfind index rindex count".split())
_REWIRING = frozenset("exec eval compile getattr setattr globals vars __import__ importlib builtins".split())
_PATH_HEAD = lazy(r"(?:/|~|\.\.?/|[\w.-]+/)")  # how a path's first piece starts; a tail like ".json" doesn't


def _fragment_ids(tree):
    """T-4138: the ids of string constants in parsed python that name no file: an argument of a str method
    (f.endswith('.jsonl'), s.split('.py')), and the tail after a computed head (n + '.json', f'{n}.json'), which the
    quote scan read as a path of its own and named in a refusal ("<project>/.json"). A head stays (home + '/x' keeps
    '/x' out: the path it ends is computed, not the literal). Code that rewires names or defines a method of that name
    is read whole, as before."""
    import ast
    nodes = list(ast.walk(tree))
    if any(isinstance(n, ast.Name) and n.id in _REWIRING for n in nodes):
        return frozenset()
    own = {n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    own |= {n.attr for n in nodes if isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Load)}

    def text(e):
        return isinstance(e, ast.Constant) and isinstance(e.value, str)
    out = set()
    for n in nodes:
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _STR_METHODS \
                and n.func.attr not in own:
            out |= {id(a) for a in n.args if text(a)}
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add) and text(n.right) and not text(n.left):
            out.add(id(n.right))
        elif isinstance(n, ast.JoinedStr):
            out |= {id(v) for v in n.values[1:] if text(v)}
            if n.values and text(n.values[0]) and not _PATH_HEAD.match(n.values[0].value):
                out.add(id(n.values[0]))
    return out


# T-4724: what os and shutil functions only look at a path (a read; os.open, os.system and the rest stay write calls)
#  T-4736: and the os calls that take a command or a program, not a file to change (system, popen, exec*, spawn*: the spawn
#  scan judges their text)
_PY_READ_CALLS = {"os": frozenset(("readlink stat lstat listdir scandir access walk statvfs getenv fspath system popen "
                                   "execv execve execvp execvpe execl execle execlp execlpe spawnv spawnve spawnvp "
                                   "spawnvpe spawnl spawnle spawnlp spawnlpe posix_spawn posix_spawnp").split()),
                  "shutil": frozenset(("which", "disk_usage"))}
_TWO_PATH_CALLS = frozenset("rename replace renames symlink link copy copy2 copyfile copytree copymode copystat move".split())
_OPEN_MODULES = frozenset("io codecs gzip bz2 lzma tarfile".split())  # name.open(file, mode): the same call as open()


def _opens_for_reading(call, pos):
    """T-4724: whether an open-like call reads: no mode, or a literal string mode holding none of w, a, x and +, at
    position `pos` or by keyword. A mode the guard can't read (a name, a built string, an expression, a * or ** that
    could carry one) and an opener (which may open with any flags) are not reads."""
    import ast
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg in (None, "opener") for k in call.keywords) \
            or len(call.args) > pos + 6:  # the 8th argument of open() is the opener
        return False
    mode = call.args[pos] if len(call.args) > pos else next((k.value for k in call.keywords if k.arg == "mode"), None)
    return mode is None or (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                            and not set(mode.value) & set("wax+"))


# T-4736: what a call on a string or bytes returns: the receiver of a later .replace(…) is then text, no pathlib path
_STR_RESULT = frozenset(("read readline readlines read_text read_bytes decode encode strip lstrip rstrip lower upper "
                         "format join split rsplit splitlines getvalue title casefold ljust rjust center zfill "
                         "removeprefix removesuffix").split())


def _is_text(e):
    """T-4736: whether an expression is text, never a pathlib path: a string or bytes literal, what a read or a str
    method returns, and str.replace of those. 'a'.replace('a', 'b') and open(f'/proc/{p}/x').read().replace(b'\\0', b' ')
    name no file to write (the walk over the receiver reached the fragment inside the read)."""
    import ast
    while isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute):
        if e.func.attr in _STR_RESULT or (isinstance(e.func.value, ast.Name) and e.func.value.id == "os"
                                          and e.func.attr in ("readlink", "getenv", "fspath")):
            return True
        if e.func.attr != "replace":
            return False
        e = e.func.value
    return isinstance(e, ast.Constant) and isinstance(e.value, (str, bytes))


def _writer_ref(e):
    """T-4736: whether an expression is a function that writes or deletes its first argument, passed on as a value
    (os.remove, shutil.rmtree, Path.unlink, io.open with a mode list): what map() would call once per item."""
    import ast
    if not isinstance(e, ast.Attribute):
        return False
    if isinstance(e.value, ast.Name) and e.value.id in ("os", "shutil"):
        return e.attr not in _PY_READ_CALLS[e.value.id]
    return e.attr in ("write_text", "write_bytes", "touch", "unlink", "rename", "replace", "mkdir", "rmdir") \
        and not (isinstance(e.value, ast.Name) and e.value.id in ("str", "bytes"))  # str.replace names no file


def _write_path_args(tree):
    """The path expression of each write call in parsed python: open(P, 'w'), P.write_text(…), P.open('w'),
    io.open(P, 'w'), os.x(P, …). T-4724: an open with no mode or a literal read mode, and os.readlink, os.stat and the
    other calls that only look at P, are reads."""
    import ast
    alias = {t.id for a in ast.walk(tree) if isinstance(a, ast.Assign) and _writer_ref(a.value)
             for t in a.targets if isinstance(t, ast.Name)}  # T-4736: rm = os.remove; rm(P)
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f, arg = n.func, None
        if isinstance(f, ast.Name) and f.id in alias:
            yield from n.args[:1]
        if n.args and (isinstance(f, ast.Name) and f.id in ("map", "starmap") or isinstance(f, ast.Attribute)
                       and f.attr in ("map", "imap", "imap_unordered", "starmap", "submit")) and (
                    _writer_ref(n.args[0]) or isinstance(n.args[0], ast.Name) and n.args[0].id == "open" and len(n.args) > 2):
            # T-4736: map(os.remove, ['a.json']), pool.map(shutil.rmtree, names), map(open, names, modes): the items are
            # the paths written (the open with a modes list: any mode may be a write)
            for it in n.args[1:2] if isinstance(n.args[0], ast.Name) else n.args[1:]:
                yield from it.elts if isinstance(it, (ast.List, ast.Tuple, ast.Set)) else [it]
        if isinstance(f, ast.Name) and f.id == "open":
            kw = {k.arg: k.value for k in n.keywords}
            arg = None if _opens_for_reading(n, 1) else (n.args[0] if n.args else kw.get("file") or n)  # open(**{…}): the call
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("os", "shutil"):
            if f.attr not in _PY_READ_CALLS[f.value.id]:  # os.unlink(P), os.mkdir(P): P is the first argument
                for a in (n.args + [k.value for k in n.keywords]) if f.attr in _TWO_PATH_CALLS else n.args[:1]:
                    yield a  # os.rename(src, DST), shutil.copy(src, DST): the target is the second
        elif isinstance(f, ast.Attribute) and f.attr in ("write_text", "write_bytes", "touch", "unlink", "rename",
                                                         "replace", "mkdir"):
            arg = None if _is_text(f.value) else f.value
        elif isinstance(f, ast.Attribute) and f.attr == "open":
            if isinstance(f.value, ast.Name) and f.value.id in _OPEN_MODULES:  # io.open(P, mode), gzip.open(P, mode)
                kw = {k.arg: k.value for k in n.keywords}
                arg = None if _opens_for_reading(n, 1) else (n.args[0] if n.args else kw.get("file") or kw.get("filename"))
            elif not _opens_for_reading(n, 0):  # P.open(mode): P is a pathlib path
                arg = f.value
        if arg is not None:
            yield arg


def _writes_at_computed_path(code):
    """T-4138: whether parsed python writes through a path it computes from text (open(n + '.json', 'w'),
    Path(f'{n}.json').write_text(…)): the guard can't read which file that is. Named by the scan as "<project>/.json"
    before; now one refusal that says so."""
    import ast
    try:
        tree = ast.parse(code)
    except Exception:
        return False
    frag = _fragment_ids(tree)
    bound = {}  # T-4736: p = Path(f'{n}.json'); p.open('w'): a name a write uses holds what it was assigned, one hop deep
    for a in ast.walk(tree):
        if isinstance(a, (ast.Assign, ast.For)) and len(bound) < 200:  # for n in sys.stdin: open(n.strip(), 'w')
            for t in a.targets if isinstance(a, ast.Assign) else [a.target]:
                if isinstance(t, ast.Name):
                    bound.setdefault(t.id, []).append(a.value if isinstance(a, ast.Assign) else a.iter)

    def run_time(c):  # text typed, passed, read or inherited when the script runs: the path is whatever it holds then
        if isinstance(c, ast.Call):
            f = c.func
            return (isinstance(f, ast.Name) and f.id in ("input", "raw_input", "getenv")
                    or isinstance(f, ast.Attribute) and f.attr in ("getenv", "read", "readline", "readlines"))
        return isinstance(c, ast.Attribute) and c.attr in ("argv", "environ", "stdin")


    def reach(arg):
        seen, todo, out = set(), [arg], []
        while todo and len(out) < 2000:
            for c in ast.walk(todo.pop()):
                out.append(c)
                if isinstance(c, ast.Name) and c.id not in seen and len(seen) < 20:
                    seen.add(c.id)
                    todo += bound.get(c.id, [])
        return out
    return any(id(c) in frag or run_time(c) for arg in _write_path_args(tree) for c in reach(arg))


def _computed_path_write(cmd):
    """Whether python in cmd (a heredoc, T-4736: or a `-c` string) writes at a path the guard can't read."""
    codes = _python_units(cmd)[0] + [c for c, _, _ in _py_runs(cmd) if c is not None]
    return any(_writes_at_computed_path(code) for code in codes)


def _text_quoted(text):
    """The quoted path literals in code the guard can't parse. T-4138: a match that opens at a quote closing an earlier
    literal on its line and holds a parenthesis is the stretch of code between two literals (b''.join(f('<I'): the
    scan goes on one character later, so a real path inside it is still found."""
    out = []
    for line in text.split("\n"):
        closers = {m.end() - 1 for q in "'\"" for m in re.finditer(f"{q}[^{q}\\n]*{q}", line)}  # either kind alone: a
        # code string inside a shell string (python3 -c "…'<I'…") pairs its single quotes among themselves
        pos = 0
        while m := _QUOTED.search(line, pos):
            if m.start() in closers and re.search(r"[()]", m.group(2)):
                pos = m.start() + 1
                continue
            out.append(m.group(2))
            pos = m.end()
    return out


def _quoted_paths(cmd):
    """T-2629: the quoted literals that could be paths. A python heredoc that parses gives its real string constants,
    each one a path, and any quoted path inside one (os.system('… > "/x"')): a quote scan over the code paired the close
    of "\\n" with the open of the next literal, so "\\n".join(out)+"\\n" read as the path .join(out)+. A literal path is
    always a constant, so nothing real is lost (comments, which write nothing, are). The shell text and code that
    doesn't parse keep the quote scan."""
    import ast
    units, rest, starters, _ = _python_units(cmd)
    units = units + [c for c, _, _ in _py_runs(cmd) if c is not None] if "-c" in cmd else units  # T-4736: `-c` code too
    out, text = [], "\n".join(rest + starters)  # review: python - '/path' <<EOF: its argv is open(sys.argv[1])'s path
    for s in starters:  # T-2634: unquoted too (python3 - ~/.claude/settings.json <<EOF), each word as the shell reads it
        m = re.search(r"python[0-9.]*\s+-\s+(.*?)\s*<<", s)
        out += [w for w in (_words(m.group(1)) if m else []) if _PATH_LITERAL.fullmatch(w) or w.startswith("$")]
    for code in units:
        try:
            tree = ast.parse(code)
        except Exception:  # SyntaxError, ValueError, and RecursionError or MemoryError on deep nesting: the quote scan
            text += "\n" + code
            continue
        skip = {id(a) for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_sys_path_call(n.func) for a in n.args}
        skip |= {id(v) for n in ast.walk(tree) if isinstance(n, ast.FormattedValue) and n.format_spec  # T-3716: the
                 for v in n.format_spec.values if isinstance(v, ast.Constant)}  # .0f of {dt:.0f}; nested code counts
        skip |= _fragment_ids(tree)  # T-4138: a str method's argument, the tail of a computed name
        for n in ast.walk(tree):  # T-2662: sys.path.insert/append(…, "/lib") names an import folder, not a write
            if id(n) not in skip and isinstance(n, ast.Constant) and isinstance(n.value, (str, bytes)):  # open(b'/x', 'w') writes /x too
                v = n.value.decode("latin-1") if isinstance(n.value, bytes) else n.value
                if _PATH_LITERAL.fullmatch(v):
                    out.append(v)
                out += [m.group(2) for m in _QUOTED.finditer(v)]
        # T-4138: a bare name a write call opens (open('x.py', 'w')) is a path too: the old quote scan named it only by
        # the accident of pairing quotes across the code in between
        for arg in _write_path_args(tree):  # only open('x') / Path('x'): os.getenv('HOME') is no file
            if isinstance(arg, ast.Call) and (getattr(arg.func, "id", None) or getattr(arg.func, "attr", None)) == "Path":
                arg = arg.args[0] if arg.args else arg
            if (isinstance(arg, ast.Constant) and isinstance(arg.value, str) and id(arg) not in skip
                    and re.fullmatch(r"[\w][\w.-]*", arg.value) and not _PATH_LITERAL.fullmatch(arg.value)):
                out.append(arg.value)
    # the same bare name in code the guard can't parse (python behind an env assignment, a -c body)
    out += [m.group(2) for m in re.finditer(r"""\bopen\(\s*(['"])(\w[\w.-]*)\1\s*,\s*['"][rbt]*[wax+]""", text)]
    return out + _text_quoted(text)


# T-1460: the home folder's credential names, as code joins them (os.path.join(HOME, ".ssh", "id_rsa"))
_CRED_PART = lazy(r"(?:^|/)(?:\.ssh|\.aws|\.gnupg|\.kube|\.config/gcloud|\.config/gh|\.config/sampo|\.netrc|\.npmrc|\.pypirc"
                        r"|\.git-credentials|\.docker|\.credentials\.json|id_(?:rsa|dsa|ecdsa|ed25519)(?!\.pub))(?:/|$)")
_HOME_IN_CODE = lazy(r"~|\bHOME\b|expanduser|homedir|Path\.home|Dir\.home|getpwuid")
# credential files by name, wherever they sit in the code (quotes can be paired wrongly on purpose)
_CRED_FILE_NAMES = lazy(r"id_(?:rsa|dsa|ecdsa|ed25519)(?![\w.]*\.pub)(?!\w)|\.netrc\b|\.git-credentials\b"
                              r"|\.credentials\.json\b|\.aws/credentials\b|\.docker/config\.json\b|\.pypirc\b|\.npmrc\b")
_PATHY = lazy(r"^[^\s()'\"`;,{}]+$")  # a literal that is one path, not a program
_CC_NAME = lazy(r"CLAUDE(?:CODE|_CODE_ENTRYPOINT)(?!\w)")  # no left edge: env -uCLAUDECODE



def _glob_reaches(pattern, target):
    """Whether a glob pattern matches target or a folder holding it, by glob's own rule: a wildcard never matches a
    leading dot (~/* isn't ~/.ssh; ~/.s* is)."""
    import fnmatch
    ps, ts = pattern.split("/"), target.split("/")
    return len(ps) <= len(ts) and all(fnmatch.fnmatchcase(t, p) and (p.startswith(".") or not t.startswith("."))
                                      for p, t in zip(ps, ts))


def _interpreter_reads(cmd, ctx):
    """T-1460: interpreter code that names a credential file in the home folder reads it as surely as cat does: a
    quoted path that resolves to one (classify_read: public parts aside), or a credential name beside the home folder
    (a path joined at run time). T-1570: code that changes CLAUDECODE or CLAUDE_CODE_ENTRYPOINT hides Claude from fm."""
    dry = _drop_data_heredocs(cmd)  # a heredoc fed to fm edit or cat is data, not code an interpreter here reads
    if not _INTERP.search(_mask_fm(dry, ctx)):
        return []
    code = _interp_code(dry)
    found, home = [], _HOME_IN_CODE.search(code) or ctx.home in code
    # a relative name opens in whichever folder the line may be in: where it started, or any folder it cds to
    bases = [ctx.cwd] + [_resolve(_expand(d, ctx), ctx.cwd) for d in re.findall(r"(?:^|[;&|(\n{])\s*cd\s+([^\s;&|)]+)",
                                                                              dry) if not _unresolvable(d)]
    lits = [m.group(1) for q in "'\"`" for m in re.finditer(f"{q}([^{q}\\n]*){q}", code)]
    # T-4736 review: a /proc path with a computed pid (f'/proc/{p}/environ', '/proc/%s/mem' % p) or one joined from parts
    # ('/proc/' + p + '/environ', os.path.join('/proc', p, 'maps')): the pid may be anyone's, `self` included
    if any("/proc" in lit for lit in lits) and (
            any("/proc/" in lit and _proc_secret(re.sub(r"\{[^{}]*\}|%[-\d.]*[sdri]", "N", lit)) for lit in lits)
            or any(re.fullmatch(r"/?(?:environ|mem|maps|smaps|smaps_rollup|fd(?:/.*)?)", lit) for lit in lits)):
        found.append(("credentials", "interpreter code reads another process's environment, memory or open files "
                      "(/proc/PID/environ, mem, maps, fd)"))
    for lit in lits:
        if not _PATHY.match(lit):
            continue
        explicit = os.path.isabs(lit) or lit.startswith(("~", "$HOME", "${HOME}"))
        for path, real in dict.fromkeys(_walk(_expand(lit, ctx), b) for b in bases):
            hits = classify_read(path, ctx) or real and classify_read(real, ctx)  # as cat's operand is (T-4667: a '..' too)
            if not hits and re.search(r"[*?\[]", path):  # a glob that can reach a credential file
                hits = ["credentials"] * any(_glob_reaches(path, os.path.join(ctx.home, d, leaf))
                                             for d in _CRED_DIRS + _CRED_FILES for leaf in ("id_rsa", "credentials"))
            # an explicit home path is judged exactly; a bare ".aws" may be joined to the home folder at run time
            if not hits and not (explicit and _under(path, ctx.home)) and _CRED_PART.search(lit) and (
                    home or re.search(r"[*?\[]", lit) or re.match(r"/(?:home|Users|root|var/home)(?:/|$)", path)):
                hits = ["credentials"]  # ponytail: a joined known_hosts is refused too; name the whole path
            found += [(cat, f"{lit!r} in interpreter code reads a credential file") for cat in hits]
    # credential file names in the code's strings and heredoc bodies (quotes can be paired wrongly on purpose), not in
    # the shell words around it (ssh -i ~/.ssh/id_ed25519 uses a key)
    named = next((m for t in lits + [_heredocs(dry)[1]] for m in [_CRED_FILE_NAMES.search(t)] if m), None)
    if home and named:
        found.append(("credentials", f"interpreter code names {named.group(0)!r} beside the home folder"))
    # ponytail: interpreter code that changes CLAUDECODE before starting fm isn't read: edit scripts carry the same
    # words as text (env={"CLAUDE_CODE_ENTRYPOINT": …} in a test they write), and fm's verbs that matter fail closed
    # off a terminal (T-1572); the shell forms are refused in _hides_claude
    return list(dict.fromkeys(found))


# T-4667: the calls that make a HARD link (a second name for one inode): os.link, Path.hardlink_to / link_to, fs.linkSync,
# File.link, FileUtils.ln, perl's and php's bare link(), `from os import link`. A symlink or a copy is a new inode: no
_LINK_API = lazy(r"\.(?:link|linkSync|linkat|hardlink_to|link_to)\s*\(|(?<![\w.$])link\s*\(|\bFileUtils\.ln\b"
                 r"|\bfrom\s+os\s+import\b[^\n]*\blink\b")


def _interp_links(cmd, ctx):
    """T-4667 (after T-4666's ln): interpreter code that hardlinks a credential gives it a second, released-looking name
    (os.link('.env', '.env.example')). Coarse like the write scan: any quoted literal in code that calls a link API is a
    possible source, judged as ln's source is (classify_hardlink, where it is and where it really goes); the new name
    can't be told from the source in text, so a hardlink NAMED `.env` is refused too (use cp)."""
    dry = _drop_data_heredocs(cmd)
    if not _INTERP.search(_mask_fm(dry, ctx)):
        return []
    code = _interp_code(dry)
    if not _LINK_API.search(code):
        return []
    bases = [ctx.cwd] + [_resolve(_expand(d, ctx), ctx.cwd) for d in re.findall(r"(?:^|[;&|(\n{])\s*cd\s+([^\s;&|)]+)",
                                                                              dry) if not _unresolvable(d)]
    found = []
    for lit in dict.fromkeys(m.group(1) for q in "'\"`" for m in re.finditer(f"{q}([^{q}\\n]*){q}", code)):
        if not _PATHY.match(lit):
            continue
        for path, real in dict.fromkeys(_walk(_expand(lit, ctx), b) for b in bases):
            found += _judge(classify_hardlink, path, real, ctx, " (hardlinked from interpreter code)")
    return list(dict.fromkeys(found))


_CC_SET = lazy(r"^(?:CLAUDECODE|CLAUDE_CODE_ENTRYPOINT)\+?=")
_CC_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
# T-4747: programs that put text into another terminal as keystrokes, and what they would put there
_TYPERS = ("tmux", "screen", "xdotool", "ydotool", "dotool", "wtype", "kitty", "kitten", "wezterm", "zellij", "byobu")
_SEND_VERBS = {"send-keys", "send", "send-key", "paste-buffer", "load-buffer", "set-buffer", "type", "key", "keydown",
               "keyup", "stuff", "send-text", "write", "paste"}  # a typer's words that put text in; reading is none
_REMOTE_RUNNERS = ("ssh", "mosh", "pct", "lxc", "docker", "podman", "kubectl", "nsenter", "su", "script", "watch",
                   "unbuffer", "expect", "sh", "bash", "zsh", "dash", "setsid", "nohup", "flock", "chroot", "systemd-run")
_SWITCH_TEXT = lazy(r"/fm-(?:risk|trust)", re.I)
_SWITCH_FIRST = lazy(r"\s*/fm-(?:risk|trust)\b", re.I)  # a prompt that starts like the command


def _inner_words(argv):
    """The words a runner (ssh box …, bash -c "…", docker exec c …) hands on: its arguments, each one that holds a
    space read as the command line it is."""
    out = []
    for w in argv:
        try:
            out += shlex.split(w) if re.search(r"\s", w) else [w]
        except ValueError:
            out.append(w)
    return out


def _after(argv, names):
    """(the program, the words after it) for the command's own program when its base name is in `names`, else for the
    first such word of a runner's arguments; None when it is neither."""
    if not argv:
        return None
    name = os.path.basename(argv[0])
    if name in names:
        return name, argv[1:]
    if name not in _REMOTE_RUNNERS:
        return None  # git commit -m 'tmux …' is only text
    inner = _inner_words(argv[1:])
    for i, w in enumerate(inner):
        if os.path.basename(w) in names:
            return os.path.basename(w), inner[i + 1:]
    return None


def _types_switch(words, shell=""):
    """T-4747: whether one simple command types /fm-risk or /fm-trust into a terminal (tmux send-keys, xdotool type, a
    paste, a write to a pty): the UserPromptSubmit hook answers those as the user's own typed words, so a tool call that
    types them would move the brake or trust in the user's place. The words are joined without a gap, so a command
    split over arguments ('/' 'fm-risk') reads whole; a typer a runner starts (ssh box tmux …, bash -c "tmux …") is read
    too, and only a word that puts text in counts (send-keys, type, paste-buffer…: reading a pane is no typing). This
    is the plain case: the channel itself is closed by the typing stamp (hooks._pre_typed), which refuses a typed
    /fm-risk for 15 minutes after any tool call that names a typer, whatever text it carries. A claude started with the command
    as its prompt (not -p) is the same forgery: its prompt reaches the hook as a person's."""
    argv, _ = _strip_wrappers(words)
    if not argv:
        return False
    typer = _after(argv, _TYPERS)
    if typer and _SWITCH_TEXT.search("".join(typer[1])) and (typer[0] == "wtype" or any(w in _SEND_VERBS for w in typer[1])):
        return True
    claude = _after(argv, ("claude",))
    if claude and not any(re.fullmatch(r"--print|-[A-Za-oq-z]*p[A-Za-z]*", w) for w in claude[1]) \
            and any(_SWITCH_FIRST.match(w) for w in claude[1]):
        return True
    return bool(_SWITCH_TEXT.search("".join(argv)) and re.search(r">+\s*/dev/(?:pts/\d+|tty\w*)", shell or ""))


# T-4747 round 4: what the typing stamp (hooks._pre_typed) reads. A command is stamped when a terminal typer is a COMMAND
# WORD of it (through bash -c, wrappers, nesting): `grep tmux notes` or `cat tmux.conf` name one as data and stamp nothing.
_TYPER_WORDS = frozenset(_TYPERS) | {"dtach", "abduco", "reptyr", "ttyecho", "script", "expect"}
_TMUXISH = ("tmux", "byobu")
_TMUX_READ = frozenset({"capture-pane", "capturep", "list-panes", "lsp", "list-sessions", "ls", "list-windows", "lsw",
                        "display-message", "display", "has-session", "has", "show-options", "show", "list-clients", "lsc",
                        "list-buffers", "lsb", "show-buffer", "showb", "show-environment", "showenv"})
_TYPER_SUBCOMMANDS = frozenset({"send-keys", "send-key", "send-text", "paste-buffer", "load-buffer", "set-buffer"})  # no program of
# these names exists: as a command word one is a typer's subcommand whose typer was computed
_TMUX_VALUED = ("-L", "-S", "-f", "-c", "-T")  # tmux's options that take a value; the rest are flags
_SCREEN_READ = ("-ls", "-list", "-v", "-wipe")
_TYPER_RUNNERS = frozenset(_REMOTE_RUNNERS) | {"find", "parallel", "doas", "pkexec", "run0", "runuser", "sg", "strace", "ltrace",
                                               "taskset", "chrt", "unshare", "firejail", "bwrap", "fakeroot", "entr", "at",
                                               "batch", "setpriv", "xargs", "cpulimit", "inotifywait", "screen", "tmux"}
_TYPER_DATA_READERS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "cat", "less", "more", "ls", "echo", "printf", "which",
                                 "whereis", "type", "man", "head", "tail", "pgrep", "pkill", "ps", "dpkg", "apt", "pacman",
                                 "git", "wc", "stat"})
_SHELL_NAME = lazy(r"(?:ba|z|da|k|fi|a|c|tc)?sh")
_INTERPRETER = lazy(r"(?:python[\d.]*|perl|ruby|node|nodejs|php|lua|deno|bun|pwsh|powershell|osascript|awk|gawk)")
_TYPER_NAME = lazy(r"(?<![\w.-])(tmux|screen|xdotool|ydotool|dotool|wtype|kitty|kitten|wezterm|zellij|byobu|dtach|abduco"
                         r"|reptyr|ttyecho|unbuffer|pexpect)(?![\w-])", re.I)
# the words that put text in: narrow on purpose (a bare type, key, send, write or paste is everyday English and code)
_NARROW_SEND = lazy(r"send-?keys?\b|send-text|paste-buffer|load-buffer|set-buffer|(?<![\w-])stuff(?![\w-])|keydown|keyup"
                          r"|\b(?:xdotool|ydotool|dotool)\b[^\n]{0,80}?\b(?:type|key)\b|(?<![\w.-])wtype(?![\w-])", re.I)
_LOW_LEVEL = lazy(r"TIOCSTI|pty\.spawn|pty\.fork|openpty|forkpty|/dev/pts/\d|/dev/uinput|(?<![\w.])(?:0x5412|21522)(?!\w)"
                        r"|pexpect", re.I)


_TYPER_CAP = 200_000  # the longest text read word by word; a longer one stamps when it names a typer at all (one linear scan)


def _names_typer(text):
    """Whether a text too long to read names a typer with a word that puts text in, or a low-level way to type."""
    return bool(_TYPER_NAME.search(text) and ("/fm-" in text or _NARROW_SEND.search(text)) or _LOW_LEVEL.search(text))


def typer_content(text):
    """T-4747 round 4: the typer names text carries as something it WOULD DO: a typer's name together with `/fm-` or one of
    the words that put text in (send-keys, paste-buffer, xdotool type…), or a low-level way to type into a pty (TIOCSTI,
    pty.spawn, /dev/uinput…). This is the rule for text a call writes for a later run (a file, a heredoc, an interpreter's
    code); a typer named alone (`# tmux is optional`, a variable called tmux_name) is not one. [] when it types nothing."""
    if len(text) > _TYPER_CAP:  # never cut: a typer past the cut would stamp nothing
        return ["big"] if _names_typer(text) else []
    out = [m.group(1).lower() for m in _TYPER_NAME.finditer(text)]
    out = list(dict.fromkeys(out)) if "/fm-" in text or _NARROW_SEND.search(text) else []
    if _LOW_LEVEL.search(text):
        out.append("pty")
    return out


_SEG_WORDS = frozenset({";", "\\;", "&&", "||", "|", "&", "+", "-exec", "-execdir", "-ok", "-okdir"})


_SHELL_META = lazy(r"[;&|\n`]|\$\(")
_CMD_LINE = lazy(r"\s|\$\(|`")
# the options with which a "data reader" runs a command of its own (git -c core.pager=…, man -P, less +!, rg --pre): such a
# reader is not exempt, and what it is given is read as a command line
_READER_RUNS = {
    "git": lazy(r"-c|--config-env(?:=.*)?|--exec-path(?:=.*)?|--upload-pack(?:=.*)?|--receive-pack(?:=.*)?|--exec(?:=.*)?"
                      r"|config|alias\..*"),  # git config writes a command into config (alias.x '!CMD'): T-4747 round 10
    "printf": lazy(r"-v.*"),  # printf -v X 'text' puts the text in a variable a later line runs
    "man": lazy(r"-P|--pager(?:=.*)?|-H|--html(?:=.*)?"),
    "less": lazy(r"\+!.*|--lesskey.*"), "more": lazy(r"\+!.*"),
    "rg": lazy(r"--pre(?:=.*)?|--hostname-bin(?:=.*)?"), "ag": lazy(r"--pager(?:=.*)?"),
    "apt": lazy(r"-o"), "dpkg": lazy(r"--(?:pre|post)-invoke(?:=.*)?"),
}
_RUNS_ENV = lazy(r"(?:LESSOPEN|LESSCLOSE|PAGER|GIT_PAGER|MANPAGER|GIT_SSH_COMMAND|GIT_EXTERNAL_DIFF|EDITOR|VISUAL|BROWSER)=.*")


def _reader_runs(seg):
    """Whether the data reader that leads `seg` is given an option that runs a command."""
    pat = _READER_RUNS.get(seg[0])
    return any(_RUNS_ENV.fullmatch(w) or (pat is not None and pat.fullmatch(w)) for w in seg[1:])


def _scan_segment(seg, depth, tmuxish=False, skip_first=False):
    """T-4747 round 6: the typers one command's words (a runner's arguments up to its next separator) hand to a program: the
    first command-like word (a typer, a shell, an interpreter, eval, another runner) is read by `_typer_cmd` with the words
    after it, so interpreters, computed words, nested sh -c and runners go through the one parser. A data reader (grep, cat…
    by bare name, with no option that runs a command) hides what follows it in its own segment: `docker exec c grep tmux`
    names a typer as data. A word that computes ($T, `…`) is read the same and the scan goes on."""
    names = []
    for i, w in enumerate(seg):
        if w in _TYPER_DATA_READERS:
            if not _reader_runs(seg[i:]):
                break
            for x in seg[i + 1:]:  # `core.pager=CMD`, `--pre=CMD`, `+!CMD`, `PAGER=CMD`: the value is a command line
                cmd = x[2:] if x.startswith("+!") else x.split("=", 1)[1] if "=" in x else ""
                if cmd:
                    names += typer_commands(cmd, depth + 1)
        if skip_first and i == 0:
            continue
        b = os.path.basename(w)
        if b in _TYPER_WORDS or (b in _TYPER_SUBCOMMANDS and not tmuxish) or b in _TYPER_RUNNERS or b == "eval" \
                or _SHELL_NAME.fullmatch(b) or _INTERPRETER.fullmatch(b):
            names += _typer_cmd(seg[i:], depth + 1)
            break
        if re.search(r"[$`]", w):
            names += _typer_cmd(seg[i:], depth + 1)
    return names


def _runner_names(args, depth, tmuxish=False):
    """T-4747 round 6: the typers a runner's arguments hand on. Three readings, all bounded by the depth cap: (1) the words
    as they are, cut at ; && || | & \\; + and find's -exec/-execdir/-ok, each cut scanned (an interpreter keeps its code
    argument whole); (2) when a word holds a separator or substitution, the arguments joined with spaces and read again as
    one command line, as the shell ssh, watch or su -c hands them to reads them (`ssh box grep x f\\;tmux send-keys`: the
    remote line is `grep x f;tmux send-keys`); (3) an argument that is itself a command line (a space, `$(`, a backtick)
    through typer_commands, which reads substitutions, subshells, bash -c and heredocs. A joined or per-argument reading
    is skipped past 50 KB of arguments (it would multiply the work for nothing): such a line stamps as "big"."""
    names, first = [], tmuxish
    args = [str(a).replace("$\\n", "\n") for a in args]  # the tokenizer leaves $'\n' as `$\n`: a newline, which cuts a command
    groups, cur = [], []
    for w in args:
        if w in _SEG_WORDS:
            groups.append(cur)
            cur = []
        else:
            cur.append(w)
    groups.append(cur)
    big = sum(len(a) for a in args) > 50_000
    for g in groups:
        if not g:
            continue
        names += _scan_segment(g, depth, tmuxish, first)
        first = False
        if big:
            continue
        if any(_SHELL_META.search(w) for w in g):
            text = " ".join(g)
            try:
                for c in _split(_tokens(_lines(text))):
                    names += _scan_segment([str(x) for x in c.argv], depth, tmuxish)
                for body in _subst_bodies(text):
                    names += typer_commands(body, depth + 1)
            except Exception:
                names.append("unparsed")
        for w in g:
            if _CMD_LINE.search(w):
                names += typer_commands(w, depth + 1)
    if big:
        names.append("big")
    return names


def _tmux_stamps(args):
    """Whether a tmux (or byobu) command line is more than reading: its first non-option word must be a literal read-only
    subcommand and no `;` may chain another on. No subcommand, a computed one or anything else stamps."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _TMUX_VALUED else 1
    if i >= len(args) or args[i] not in _TMUX_READ:
        return True
    return any(";" in a for a in args[i:])


def _typer_cmd(argv, depth):
    """The typer names one simple command (argv) types with, [] when none."""
    if not argv:
        return []
    if depth > 4:
        return ["deep"]  # runners nested past what a line can sensibly do: stamps, and bounds the work
    dropped = []
    argv, _ = _strip_wrappers(argv, dropped)
    names = ["unbuffer"] if any(os.path.basename(d) == "unbuffer" for d in dropped) else []
    if not argv or (any(os.path.basename(d) == "command" for d in dropped) and any(d in ("-v", "-V") for d in dropped)):
        return names  # command -v tmux asks where it is
    w0, args = argv[0], argv[1:]
    base = os.path.basename(w0)
    if base in _TMUXISH:
        if _tmux_stamps(args):
            names.append(base)
    elif base == "screen":
        if not args or args[0] not in _SCREEN_READ:
            names.append("screen")
    elif base in _TYPER_WORDS:
        names.append(base)
    elif base in _TYPER_SUBCOMMANDS:  # `$(echo tmux) send-keys x` splits to ["$"], ["echo","tmux"], ["send-keys","x"]
        names.append("computed")
    elif re.search(r"[$`]", w0):  # a computed command word: only when what it is given is a typer's sending words
        joined = " ".join(map(str, args))
        if "/fm-" in joined or _NARROW_SEND.search(joined):
            names.append("computed")
    elif _SHELL_NAME.fullmatch(base):
        for s in _shell_c(args):
            names += typer_commands(s, depth + 1)
    elif base == "eval":
        names += typer_commands(" ".join(map(str, args)), depth + 1)
    elif _INTERPRETER.fullmatch(base):
        names += [f"{base}:{n}" for n in typer_content(" ".join(map(str, args)))]
    if base in _TYPER_RUNNERS and base not in _TYPER_WORDS or base in _TMUXISH + ("screen",):
        names += _runner_names(args, depth, base in _TMUXISH + ("screen",))
    return names


_PIPE_FILTERS = frozenset({"sort", "uniq", "cut", "tr", "nl", "tac", "column", "fold", "rev", "expand", "unexpand"})


def _bare_reader(argv):
    """Whether a command is a data reader that runs nothing: a bare-name reader (or a plain filter) with no option that runs one."""
    return bool(argv) and (argv[0] in _PIPE_FILTERS or argv[0] in _TYPER_DATA_READERS and not _reader_runs(list(argv)))


def typer_commands(cmd, depth=0, consumed=False):
    """T-4747 round 4: the terminal typers a Bash command (or Monitor's) would type with, by name; [] when none. A typer
    counts when it is a COMMAND WORD (read the way the guard reads one: through bash -c, wrappers, runners, command
    substitutions and heredocs, to a depth of 4: deeper reads ["deep"], which stamps), never as an argument of grep or cat.
    tmux and screen are typers unless the subcommand is a literal read-only one (capture-pane, list-sessions…): a computed
    one stamps. A word that is computed stamps only when the words it is given put text in. The text a command writes
    for a later run (a heredoc body, a redirect's text, an interpreter's code) goes by typer_content. Anything unreadable
    stamps ["unparsed"]."""
    if depth > 4:
        return ["deep"]
    if len(cmd) > _TYPER_CAP:  # never cut (a typer after the cut would slip through): too long to read, it stamps if it names one
        return ["big"] if _TYPER_NAME.search(cmd) or "/fm-" in cmd or _NARROW_SEND.search(cmd) or _LOW_LEVEL.search(cmd) else []  # a command word may be computed
    try:
        plain, bodies = _heredocs(cmd)
        names = [f"heredoc:{n}" for n in typer_content(bodies)] if bodies else []
        cmds = _split(_tokens(_lines(plain)))  # a newline between commands cuts like `;` (was read as a space: T-4747 round 6)
        eaten = [False] * len(cmds)  # whether another program takes the command's output (T-4747 round 8)
        proc = any(x.procsub for x in cmds)  # a <( ) or >( ) body: what runs inside it is read by the command that holds it
        for i in range(len(cmds) - 1, -1, -1):
            eaten[i] = consumed or (proc and cmds[i].depth > 0) or (
                i + 1 < len(cmds) and cmds[i + 1].op.startswith("|") and not (_bare_reader(cmds[i + 1].argv) and not eaten[i + 1]))  # "|(" is a piped group
        for c, ate in zip(cmds, eaten):
            names += _typer_cmd(list(c.argv), depth)
            stripped, _ = _strip_wrappers(list(c.argv))
            for w in c.argv[:len(c.argv) - len(stripped)]:  # the text of a leading assignment (X='tmux send-keys …'; $X) is read: round 9
                k = w.find("=") if isinstance(w, str) else -1
                if k > 0 and w[:k].rstrip("+").isidentifier():
                    names += [f"assign:{n}" for n in typer_content(w[k + 1:])]
                    if re.search(r"\s", w[k + 1:]):
                        names += typer_commands(w[k + 1:], depth + 1)
            lead = 0
            while lead < len(c.argv) and isinstance(c.argv[lead], str) and (k := c.argv[lead].find("=")) > 0 and c.argv[lead][:k].rstrip("+").isidentifier():
                lead += 1  # NAME=value words before the command word are not it: X=1 grep tmux f is grep's
            argv = c.argv[lead:]
            reader = _bare_reader(argv)  # a data reader: by bare name (/tmp/grep is not one), without an option that runs a command
            for op, w in c.feeds:  # a here-string is the text a shell or interpreter runs (bash <<< 'tmux send-keys …'): T-4747 round 7
                if op == "<<<" and isinstance(w, str) and w and not (reader and not ate):
                    names += [f"herestring:{n}" for n in typer_content(w)] + typer_commands(w, depth + 1)
            if argv and (not reader or ate):  # an option that runs a command (sed s///e, rsync -e, tar --to-command, vim -c)
                words = [a for a in argv[1:] if isinstance(a, str)]
                if reader:  # its output is run or read as a program (echo '…' | bash, $(echo '…')): the words it prints are the command line
                    names += [f"out:{n}" for n in typer_content(" ".join(words))]
                for a in words:
                    if a != "$" and re.search(r"\s", a):
                        names += [f"arg:{n}" for n in typer_content(a)] + (typer_commands(a, depth + 1) if reader else [])
            if (c.redirs or os.path.basename(argv[0] if argv else "") == "tee") and argv:
                names += [f"write:{n}" for n in typer_content(" ".join(map(str, argv[1:])))]
        for body in _subst_bodies(plain):
            names += typer_commands(body, depth + 1, consumed=True)  # a substitution's output is used by the command around it
        if _LOW_LEVEL.search(cmd):
            names.append("pty")
    except Exception:
        return ["unparsed"]
    return list(dict.fromkeys(names))


def _hides_claude(words):
    """T-1570: whether one simple command names CLAUDECODE or CLAUDE_CODE_ENTRYPOINT to unset, empty or override it,
    or unsets a name it computes: fm reads them to know Claude, not the user, runs it. ponytail: a whole fresh
    environment (env -i, exec -c, sudo) isn't refused: clean-env test runs need it, and the fm verbs that matter fail
    closed off a terminal (T-1572)."""
    argv, _ = _strip_wrappers(words)
    lead = words[:len(words) - len(argv)]
    name = os.path.basename(argv[0]) if argv else ""
    ops = argv[1:]
    named = [a for a in ops if a in _CC_VARS or _CC_SET.match(a)]
    if any(_CC_SET.match(w) for w in lead + (argv if not argv or name in ("export", "readonly") else [])):
        return True
    if name == "unset" and (named or any(re.search(r"[$`]", a) for a in ops)):  # unset $v: a name it computes
        return True
    if name in ("local", "read", "mapfile", "readarray", "getopts") and named or \
            name in ("for", "select") and ops[:1] and ops[0] in named:  # T-3683: for V in sets V (a word after in doesn't)
        return True
    if name in ("declare", "typeset", "local") and any(_CC_NAME.search(a) for a in ops) and not any(
            a.startswith("-") and "p" in a for a in ops):  # declare -n r=CLAUDECODE makes r the variable
        return True
    if name == "let" and any(_CC_NAME.search(a) and "=" in a for a in ops):
        return True
    if any("{" in w and "CLAUDE" in w for w in words) and (  # unset CLAUDE{,CODE}: a brace expands to the name
            name in ("unset", "export", "declare", "typeset", "local", "let", "read")
            or any(os.path.basename(w) == "env" for w in words)):
        return True
    if name == "printf" and any(a == "-v" and b in _CC_VARS for a, b in zip(ops, ops[1:])):
        return True
    if name == "export" and named and any(a == "-n" or a.startswith("+") for a in ops):
        return True
    for i, w in enumerate(words):
        if os.path.basename(w) == "env":  # env's own words: options, their values, assignments, wherever a wrapper
            vals, j = [], i + 1           # the guard doesn't know (chrt, taskset…) put it (T-1722)
            while j < len(words) and (words[j].startswith("-") or "=" in words[j]):
                vals.append(words[j])
                if words[j] in ("-u", "-C", "-S", "--unset", "--chdir", "--split-string") and j + 1 < len(words):
                    vals.append(words[j + 1])
                    j += 1
                j += 1
            if any(_CC_NAME.search(o) for o in vals) or any(
                    a in ("-u", "--unset") and re.search(r"[$`]", b) for a, b in zip(vals, vals[1:])):
                return True
    return False


_CONFINE_SET = lazy(r"^(?:FOREMAN|SAMPO)_CONFINE\+?=")


def _unconfines(words):
    """T-1607: whether one simple command sets FOREMAN_CONFINE, the user's switch that runs fm code workers without
    Landlock: as a lead assignment, through env, or with export/declare and kin. ponytail: read/printf -v/eval forms
    aren't caught; confinement is a guard against mistakes, and the user turns it off in their own shell."""
    argv, _ = _strip_wrappers(words)
    lead = words[:len(words) - len(argv)]
    name = os.path.basename(argv[0]) if argv else ""
    if any(_CONFINE_SET.match(w) for w in lead):
        return True
    return name in ("export", "readonly", "declare", "typeset", "local", "env") and any(
        _CONFINE_SET.match(a) for a in argv[1:])


# T-1575: awk runs commands from its program: system(…), print … | "cmd", "cmd" | getline; and writes print > "file"
_AWKS = {"awk", "gawk", "mawk", "nawk", "original-awk"}
_AWK_STR = r'"(?:[^"\\\n]|\\.)*"'
_AWK_TERM = rf'(?:{_AWK_STR}|\$\d+\b|\$?[A-Za-z_]\w*\b(?:\[[^\]\n]*\])?)'  # \b: a word is one term (no 2^n splits)
_AWK_EXPR = rf'{_AWK_TERM}(?:[ \t]*{_AWK_TERM})*'
_AWK_TAKES = {"-F", "-v", "-f", "-E", "-e", "-i", "-W", "-l", "--file", "--exec", "--source", "--include", "--assign",
              "--field-separator", "--load"}
_AWK_LONG = ("--file", "--exec", "--source", "--include", "--assign", "--field-separator", "--load")


def _awk_sources(args, cwd):
    """T-1575: (program texts, -v assignments, data operands, whether a -f program couldn't be read, whether gawk
    edits the data files in place) of an awk command line; a -f file is read when it is there and small."""
    progs, assigns, i, from_file, unread, inplace = [], {}, 0, False, False, False
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        if a == "--":
            i += 1
            break
        opt, val = (a.split("=", 1) + [""])[:2] if a.startswith("--") else (a[:2], a[2:])
        if opt.startswith("--") and len(opt) > 3:  # gawk takes a unique prefix: --sou is --source
            opt = next((o for o in _AWK_LONG if o.startswith(opt)), opt)
        if opt in _AWK_TAKES and not val and i + 1 < len(args):
            i += 1
            val = args[i]
        if opt == "-W":  # -W source=…, -W file=…, mawk's -W exec FILE: the long option by another name
            w, eq, wv = val.partition("=")
            opt = next((o for o in _AWK_LONG if len(w) > 1 and o.startswith("--" + w)), "-W")
            if opt == "--exec" and not eq and i + 1 < len(args):
                i += 1
                wv = args[i]
            val = wv
        if opt in ("-i", "--include") and re.sub(r"\.awk$", "", os.path.basename(val)) == "inplace":
            inplace = True
        if opt in ("-f", "--file", "-E", "--exec"):
            from_file, path, unread = True, _resolve(val, cwd), True
            try:
                if os.path.isfile(path) and os.path.getsize(path) <= 65536:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        progs.append(f.read())
                    unread = False
            except OSError:
                pass
        elif opt in ("-e", "--source"):
            progs.append(val)
        elif opt in ("-v", "--assign") and "=" in val:
            k, v = val.split("=", 1)
            assigns[k] = ['"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"']
        i += 1
    rest = args[i:]
    if not from_file and not progs and rest:  # -f and -e stand in for the program operand
        progs, rest = [rest[0]], rest[1:]
    return progs, assigns, rest, unread, inplace


def _awk_value(expr, assigns, seen=()):
    """The text an awk expression builds: its strings as they are, a variable set once to an expression of them by
    that expression, anything else (fields, input, a variable set more than once) as $AWKVAR, a value the guard
    reads as computed."""
    out = []
    for t in re.findall(_AWK_TERM, expr):
        if t.startswith('"'):  # \n \t, octal \162 and hex \x72 escapes, any other \c as c
            out.append(re.sub(r"\\([0-7]{1,3}|x[0-9a-fA-F]{1,2}|.)", lambda m: chr(int(m.group(1), 8)) if m.group(1)[0]
                              in "01234567" else chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "x" and len(
                              m.group(1)) > 1 else {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), t[1:-1]))
        elif len(assigns.get(t, ())) == 1 and t not in seen:
            out.append(_awk_value(assigns[t][0], assigns, seen + (t,)))
        else:
            out.append("$AWKVAR")
    return "".join(out)


def _balanced(text, start):
    """The text from start up to the parenthesis that closes the one just before it (strings skipped)."""
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == '"':
            m = re.match(_AWK_STR, text[i:])
            i += len(m.group(0)) if m else 1
            continue
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1 if not depth else i]


def _check_awk(args, ctx, depth, cwd, cmd):
    """T-1575: what an awk program runs or writes, read as the commands and paths they are. A -f program the guard
    can't read (/dev/stdin, a file this line writes first, one gawk loads) is looked for in the whole line, heredocs
    and here-strings included."""
    progs, assigns, data, unread, inplace = _awk_sources(args, cwd)
    if unread:  # its heredocs, here-strings and quoted programs; not every word of the line (grep 'system(' x)
        progs += [_heredocs(cmd)[1]] + [a or b for a, b in re.findall(
            r"'([^']*\{[^']*)'|\"((?:[^\"\\]|\\.)*\{(?:[^\"\\]|\\.)*)\"", cmd)]
    inplace = inplace or any(re.search(r'@include\s+"inplace(?:\.awk)?"', p) for p in progs)
    for prog in list(progs):  # @include "file" loads another program; read it like -f
        for inc in re.findall(r'@include\s+"([^"\n]+)"', prog):
            path = _resolve(_expand(inc, ctx), cwd)
            try:
                if os.path.isfile(path) and os.path.getsize(path) <= 65536:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        progs.append(f.read())
            except OSError:
                pass
    found = []
    for d in data if inplace else ():  # gawk -i inplace rewrites each data file
        found += [f for f in _judge(classify_write, *_walk(_expand(d, ctx), cwd), ctx, " (edited in place by gawk)")
                  if f[0] in _GUARDED_BY_PATH + ("system",)]
    for prog in progs:
        for m in re.finditer(rf"(?<![=!<>])\b([A-Za-z_]\w*)[ \t]*=(?!=)[ \t]*({_AWK_EXPR})", prog):
            assigns.setdefault(m.group(1), []).append(m.group(2))
        runs = [_awk_value(_balanced(prog, m.end()), assigns) for m in re.finditer(r"\bsystem[ \t]*\(", prog)]
        for v in list(runs):  # a command built from its input (system($0)) runs the input as a script
            first = (v.split() or [""])[0]
            if first == "$AWKVAR" or (_SHELLS.match(os.path.basename(first)) and "$AWKVAR" in v):
                runs.append(f"cat {' '.join(shlex.quote(d) for d in data) or '-'} | sh")
        for _ in range(3):  # ("sh"), ("rm -rf ~"), (path): a parenthesised operand is the operand
            prog = re.sub(rf"(?<![\w\]])\(\s*({_AWK_STR}|[A-Za-z_]\w*\b)\s*\)", r"\1", prog)
        runs += [_awk_value(m.group(1), assigns) for m in re.finditer(rf"({_AWK_EXPR})[ \t]*\|&?[ \t]*getline", prog)]
        for m in re.finditer(rf"\b(?:print|printf)\b([^;{{}}\n|]*?)(?<![|&])\|&?(?!\|)[ \t]*(?!getline\b)({_AWK_EXPR})",
                             prog):
            target = _awk_value(m.group(2), assigns)
            first = (target.split() or [""])[0]
            if _SHELLS.match(os.path.basename(first)) or first == "$AWKVAR":  # what it prints is the script
                text = _awk_value(m.group(1), assigns) if m.group(1).strip() else "$AWKVAR"
                runs.append(text if "$AWKVAR" not in text else
                            f"cat {' '.join(shlex.quote(d) for d in data) or '-'} | {target}")
                runs += [_awk_value(s, assigns) for s in re.findall(_AWK_STR, m.group(1))]  # printf("%s", "…")
            runs.append(target)
        for run in runs:
            found += _child_check(run, ctx, depth + 1)  # awk's system() text is run by a child sh
        for m in re.finditer(r"\breadfile[ \t]*\(", prog):  # gawk -l readfile: readfile("path") reads it whole
            path = _awk_value(_balanced(prog, m.end()), assigns)
            if "$AWKVAR" not in path and path:
                found += _judge(classify_read, *_walk(_expand(path, ctx), cwd), ctx, " (read by awk)")
        for m in re.finditer(rf"\b(?:print|printf)\b[^;{{}}\n>|]*?>>?[ \t]*({_AWK_EXPR})", prog):
            path = _awk_value(m.group(1), assigns)
            if "$AWKVAR" not in path and path:  # ponytail: a computed file name (print > $1) isn't read
                found += [f for f in _judge(classify_write, *_walk(_expand(path, ctx), cwd), ctx, " (written by awk)")
                          if f[0] in _GUARDED_BY_PATH + ("system",)]
        for m in re.finditer(rf"\bgetline\b[ \t]*(?:[A-Za-z_]\w*[ \t]*)?<[ \t]*({_AWK_EXPR})", prog):
            path = _awk_value(m.group(1), assigns)
            if "$AWKVAR" not in path and path:
                found += _judge(classify_read, *_walk(_expand(path, ctx), cwd), ctx, " (read by awk)")
    return found


def fm_calls(cmd):
    """The argument lists of every direct `fm …` (or `python3 …/fm …`) call in a shell command."""
    calls = []
    for c in _split(_tokens(_lines(_strip_heredocs(cmd)))):
        argv, _ = _strip_wrappers(c.argv)
        name, args = (os.path.basename(argv[0]) if argv else ""), argv[1:]
        if name in FM_PROGRAMS:
            calls.append(args)
        elif re.match(r"^python[0-9.]*$", name) and args[:1] and args[0].endswith(FM_SCRIPT_ENDS):
            calls.append(args[1:])
    return calls


# T-0974: programs that only read, each with the flags that would make it write, run something or never end. A command
# passes read_only() only when every part is one of these without those flags.
_READS = {
    "ls": (), "cat": (), "head": (), "tail": ("-f", "-F", "--follow"), "wc": (), "pwd": (), "which": (), "file": ("-C", "--compile"),
    "stat": (), "du": (), "df": (), "tree": ("-o", "-R"), "grep": (), "egrep": (), "fgrep": (),
    "rg": ("--pre", "--pre-glob", "--hostname-bin", "-z", "--search-zip"),
    "sort": ("-o", "--output", "--compress-program"), "uniq": (), "cut": (), "nl": (), "diff": (), "cmp": (), "realpath": (),
    "file": ("-C", "--compile"), "basename": (), "dirname": (), "echo": (), "printf": (), "true": (), "date": ("-s", "--set"), "tr": (),
    "column": (), "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
                                     "-fprintf", "-fls"),
}
_GIT_READS = {"status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "describe", "shortlog", "grep"}
# T-3713: Sampo verbs that only read or record its state (never lane, tidy, or evidence --run, which runs a command)
_FM_KEEPS = {"capture", "decide", "status", "which", "find", "outline", "queue", "inbox", "next", "focus"}
_FM_TASK_KEEPS = {"log", "new", "set", "show", "list"}
_GIT_BAD = ("-o", "--output", "--ext-diff", "--textconv", "--open-files-in-pager", "-O")


def _bad_flag(args, bad):
    """True when an argument is one of the bad flags: a long option counts when it abbreviates one (GNU and git accept
    --comp=), a single-dash cluster when it holds a bad short letter anywhere (-uo, -oFILE, -nOcat); a bad flag with a
    longer single-dash name (find -exec) matches exactly."""
    for a in args:  # past a '--' too: it ends getopt's options, not find's expression (find -- . -delete)
        if a == "--":
            continue
        if a.startswith("--"):
            if any(b.startswith("--") and b.startswith(a.partition("=")[0]) for b in bad):
                return True
        elif a.startswith("-") and any(a == b or len(b) == 2 and b[1] in a[1:] for b in bad if b[:1] == "-" and b[1:2] != "-"):
            return True
    return False


def read_only(cmd):
    """T-0974: True when the whole command only reads: every part a _READS program (or a reading git subcommand)
    without its writing flags, nothing redirected but to /dev/null, nothing substituted, expanded, backgrounded or
    assigned.
    Conservative on purpose: anything it doesn't know gets Claude Code's own dialog."""
    if not cmd.strip() or any(x in cmd.strip() for x in ("`", "$", "<(", ">(", "\n")):  # review: $VAR can print a secret
        return False
    parts = _split(_tokens(cmd))
    for x in parts:
        if x.procsub or x.feeds or x.depth or any(r != "/dev/null" for r in x.redirs) or x.op not in ("", ";", "&&", "||", "|"):
            return False
        argv = x.argv
        if not argv or "=" in argv[0] or "/" in argv[0]:
            return False
        if argv[0] == "git":
            sub = argv[1] if len(argv) > 1 else ""
            if sub not in _GIT_READS or _bad_flag(argv[2:], _GIT_BAD if sub == "grep" else _GIT_BAD[:4]):
                return False
            continue
        bad = _READS.get(argv[0])
        if bad is None or _bad_flag(argv[1:], bad):
            return False
        if argv[0] == "uniq" and len([a for a in argv[1:] if a == "-" or not a.startswith("-")]) > 1:  # uniq IN OUT writes OUT
            return False
    return bool(parts)


def _keeps_folders(c, argv):
    """T-1924 review: True when a command can't remove, rename or relink a folder, so a later cd into one that exists
    still lands (`mv ../d ../e; cd ../d` stays where it was): cd, mkdir without a mode, a _READS program or reading git,
    substituting only reads, nothing fed or process-substituted."""
    text = " ".join(list(c.argv) + list(c.redirs))
    if c.feeds or c.procsub or re.search(r"`|[<>]\(", text) or not all(
            read_only(b) for b in _subst_bodies(text, tails=False)):
        return False
    if not argv or argv[0] in ("cd", "pushd", "popd"):
        return True
    if argv[0] == "mkdir":
        return not any(a.startswith(("-m", "--mode")) for a in argv[1:])
    if argv[0] == "git":
        return argv[1:2] != [] and argv[1] in _GIT_READS and not _bad_flag(argv[2:], _GIT_BAD)
    if argv[0] in ("sampo", "fm"):  # T-3713: Sampo's record-only verbs remove no folder
        return argv[1:2] == ["task"] and argv[2:3] != [] and argv[2] in _FM_TASK_KEEPS or argv[1:2] != [] and \
            argv[1] in _FM_KEEPS
    bad = _READS.get(argv[0])
    return bad is not None and not _bad_flag(argv[1:], bad)


def lone_fm_call(cmd):
    """The arguments of a Sampo call when it is the whole command (nothing chained, piped, redirected or substituted)."""
    if "`" in cmd or "$(" in cmd or "\n" in cmd.strip():
        return None
    cmds = _split(_tokens(_strip_heredocs(cmd)))
    if len(cmds) != 1 or cmds[0].redirs or cmds[0].procsub:
        return None
    calls = fm_calls(cmd)
    return calls[0] if len(calls) == 1 else None


def is_rule_list(cmd):
    """T-4154: the command is exactly `sampo rule list` (or `fm rule list`): the bare name, found on PATH, nothing
    before it (no wrapper, assignment, path or interpreter), nothing after it (no flag, quote, operator or expansion)."""
    return re.fullmatch(r"(?:sampo|fm) +rule +list", cmd.strip(" \t")) is not None


def lone_fm_ask(cmd):
    """The arguments of `fm ask …` when it is the whole command (nothing chained, piped, redirected or substituted):
    a permission prompt then approves exactly that request."""
    call = lone_fm_call(cmd)
    return call if call and call[:1] == ["ask"] else None


# T-0151: variables are resolved only in a straight-line command (simple commands joined by ; or newlines): a pipe, &&,
# ||, &, a subshell, a group or a control keyword can make an assignment conditional or local, so then none are
_BRANCHY = lazy(r"[|&(){}]|\b(?:if|then|else|elif|fi|for|while|until|do|done|case|esac|select|function|coproc)\b")
# T-0161: only a builtin can change a variable of this shell (an alias or a function needs one first), so resolving stops
# at any builtin outside the inert ones (cd and pushd set only names that are never resolved; [[ -eq assigns)
# T-3600 review 4: bash's own builtins and keywords (bash 5.3: compgen -b, compgen -k), frozen; test_guard_verb.BashWords
# fails when a bash has one these lack
BASH_BUILTINS = frozenset(". : [ alias bg bind break builtin caller cd command compgen complete compopt continue declare "
                          "dirs disown echo enable eval exec exit export false fc fg getopts hash help history jobs kill "
                          "let local logout mapfile popd printf pushd pwd read readarray readonly return set shift shopt "
                          "source suspend test times trap true type typeset ulimit umask unalias unset wait".split())
BASH_KEYWORDS = frozenset("! [[ ]] { } case coproc do done elif else esac fi for function if in select then time until "
                          "while".split())
_BUILTINS = BASH_BUILTINS | {"[["}
_INERT = set(": [ cd dirs echo exit false hash help jobs kill popd pushd pwd shift test times true type ulimit umask".split())
_ASSIGN = lazy(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def _setter(name, args):
    """A builtin that can set this shell's names. T-0370: printf only as printf -v NAME (the option comes first, before
    any --), so a plain printf no longer makes every name unknown."""
    if name == "printf":  # its security review: a first word bash computes ($X, `…`, {a,b}, a glob) may become -v
        first = args[0] if args else ""
        word, m = _unquote(first), re.search(r"[*?\[{]", first)  # a glob or brace makes -v… only from a -v prefix
        return bool(args) and (word is None or "$" in first or "`" in first or word.startswith("-v")
                               or bool(m) and "-v".startswith(first[:m.start()]))
    return name in _BUILTINS and name not in _INERT
_ASSIGNISH = lazy(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?\+?=")  # NAME=, NAME+= and NAME[i]=
# bash sets these itself, ignores an assignment to them or makes it fail (readonly)
_SHELL_SET = lazy(r"^(?:_|PWD|OLDPWD|DIRSTACK|BASH\w*|RANDOM|SRANDOM|SECONDS|LINENO|EPOCH\w+|HISTCMD|PPID|E?UID|"
                        r"GROUPS|FUNCNAME|PIPESTATUS|OPT\w+|REPLY|MAPFILE|COPROC\w*|SHELLOPTS|SHLVL|HOST\w+|MACHTYPE|"
                        r"OSTYPE|COMP_\w+|READLINE_\w+|COLUMNS|LINES)$")


def _straight_line(shell):
    s = re.sub(r"\$\{\w+\}|\d*>&\d*-?|&>>?", " ", shell)  # ${VAR}, 2>&1 and &> aren't branches
    if "&&" in s:  # T-0175: in a chain joined only by &&, a command runs only after every earlier one ran; mixed with
        s = s.replace("&&", " ")  # ; or a newline, one can be skipped and the next still run (false && D=x; rm $D)
        if re.search(r"[;\n]", s.strip()):
            return False
    return not _BRANCHY.search(s)


def _with_vars(tok, env):
    return re.sub(r"\$(?:\{(\w+)\}|(\w+))", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), tok)


_WORD = lazy(r"[^\s'\"`;|&<>()=]+")
_PART = lazy(r"\$(?:\{(\w+)\}|(\w+))|\$\{[^}]*\}?|\$\([^)]*\)?|`[^`]*`?|\$")  # what a shell fills in at run time


def _only_unset(tok, bare):
    """T-0374: every part a shell fills in is a name the line neither sets nor inherits: bash makes each empty."""
    return bare is not None and not any(not (n := m.group(1) or m.group(2)) or n in bare or _SHELL_SET.match(n)
                                        for m in _PART.finditer(tok))


def _line_words(shell, known):
    """T-0190: the words a line names, through the variables known here (a variable's name is no guess, its value is)."""
    text = re.sub(r"(?<![\w$])[A-Za-z_]\w*\+?=", " ", shell)
    return [w for w in dict.fromkeys(_with_vars(w, known) for w in _WORD.findall(text)) if not _unresolvable(w)]


_COMP_BODY = r"(?:(?!\b(?:for|while|until|select|if|case|done|fi|esac)\b)[^{}])*?"
_COMP_END = r"(?<![\\<>])[;\n&]\s*"  # an unescaped separator, not a redirect's (>&)
# T-1920: a for/while/until/select/if/case/{ } compound with none inside it, where a command starts: the start, an
# unescaped ; & | ( or newline (not >& >| <(), or `{ `, then any do/then/else/elif/time words; it runs in this shell
_COMPOUND = lazy(
    rf"(^|(?<![\\<>])[;&|(\n]|(?<![\\\w$])\{{(?=\s))((?:\s*\b(?:do|then|else|elif|time)(?=\s))*\s*)"  # whole words:
    rf"(?:\b(?:for|while|until|select)(?=\s){_COMP_BODY}{_COMP_END}done|\bif(?=\s){_COMP_BODY}{_COMP_END}fi"  # for-x
    rf"|\bcase(?=\s){_COMP_BODY}{_COMP_END}esac|\{{\s{_COMP_BODY}{_COMP_END}\}})(?=$|[\s;&|)])")  # is a command


def _compound_moves(m, shell):
    """T-1920 review: whether a compound _top matched may move the shell, read as written (a quoted "cd", c\\d, a computed
    command word or a substitution may): _moves_text on it, its own { } and its ${name}s aside."""
    t = shell[m.start() + len(m[1]) + len(m[2]):m.end()]
    return _moves_text(re.sub(r"\$\{\w+\}", "$V", t[1:-1] if t[:1] == "{" else t))


def _top(shell):
    """(straight, and_chain, mixed) at the command's top level, command substitutions blanked: straight when every command
    there runs in this shell, in order (no branch, subshell, pipe, background or !: `! cd BAD && x` runs x); and_chain
    when only && joins them, so a failed cd stops the rest."""
    blank = lambda m: "S" * len(m[0])  # noqa: E731 — same length throughout: T-1920 reads shell beside the masked text
    s, prev = re.sub(r"`[^`]*`", blank, shell), None
    while s != prev:
        s, prev = re.sub(r"\$\([^()]*\)", blank, s), s
    q = _mask_quotes(s)  # T-0384: a quoted ( | or ; is text
    if q != s or not re.search(r"['\"#]", s):  # T-1920: not when quote masking failed closed ($'…', an open quote)
        q, prev = re.sub(r"\$\{\w+\}", lambda m: "V" * len(m[0]), q), None
        while q != prev:  # innermost first; one that may move the shell stays branchy (a loop repeats its cd)
            q, prev = _COMPOUND.sub(lambda m: m[0] if _compound_moves(m, shell) else
                                    m[1] + m[2] + "C".ljust(len(m[0]) - len(m[1]) - len(m[2])), q), q
    s = q
    straight = "!" not in s and _straight_line(s)
    # T-0339: a ; list with && in it: each &&-segment is a chain of its own, the segments run in order; T-0355: its
    # elements may be pipelines (a pipe runs in subshells, so only a cd outside one moves the shell); T-1891: or ||
    # (a new segment too; the caller never takes a cd after || as certain: `true || cd X && y` runs y here)
    mixed = not straight and "!" not in s and _straight_line(re.sub(r"&&|\|\||(?<!\|)\|&?(?!\|)", ";", s))
    return straight, straight and "&&" in s, mixed


_DATA_TOOLS = {"curl", "wget", "head", "tail", "echo", "printf", "jq", "cat", "wc", "grep", "cut", "tr", "sleep", "true",
               "nvidia-smi",  # T-0397: a GPU query (its -f/--filename, which logs to a file, voids it)
               "sed", "docker"}  # T-3807: sed printing (_sed_prints), docker's read-only verbs (_docker_reads)
_SED_FLAGS = {"-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended", "-s", "--separate", "-u", "--unbuffered",
              "-z", "--null-data"}
# one sed command that prints and writes nothing: an address (a line, $, /re/, a range) then p, d, = or q, or an s///
# whose flags are only g, p, I and a count (w FILE and e write and run)
_SED_ADDR = r"(?:\d+|\$|/(?:\\.|[^/\\])*/)"
_SED_SAFE = lazy(rf"\s*(?:{_SED_ADDR}(?:\s*,\s*{_SED_ADDR})?\s*)?(?:[pd=]|q\d*|s(?P<d>[^\s\\\n])(?:\\.|(?!(?P=d)).)*"
                 rf"(?P=d)(?:\\.|(?!(?P=d)).)*(?P=d)[gpI\d]*)\s*")
_DOCKER_READS = {"port", "ps", "inspect", "logs", "images", "version", "info", "top"}
# flags that write no file; T-0397: -k -i -I -v (stdout or stderr only) and a timeout fused to them (-sSm5)
_CURL_SAFE = lazy(r"-[sSLfkiIv]*m\d+(?:\.\d+)?|-[sSLfkiIv]+|--(?:silent|show-error|location|fail|compressed|insecure|"
                        r"include|head|verbose|(?:max-time|connect-timeout)=\d+(?:\.\d+)?)")
_CURL_TIMES = lazy(r"-[sSLfkiIv]*m|--max-time|--connect-timeout")  # T-0397: take a number next
_WGET_SAFE = {"-q", "--quiet", "-nv"}
_RUNS = lazy(r"\$\(|`|\$\{[^}]*=")  # T-0414: a word that runs a command or sets a name as it expands
_REACHES_PYTHON = lazy(r"PYTHON\w*|LD_\w*|PATH|HOME|ENV|BASH_ENV|IFS")  # read by python, its loader or the shell


_BRACES = lazy(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")  # a brace expansion bash would turn into several words


# T-4154: the read verbs of Sampo that print and write nothing in the folder python runs in (never evidence --run, lane,
# tidy: _FM_KEEPS also holds verbs that record), with the only flags they take here
_FM_PRINTS = {("task", "show"), ("task", "list"), ("status",), ("next",), ("queue",), ("inbox",)}
_READ_ONLY_NAMES = (set(_READS) | {"git", "sampo", "fm"}) - {"which"}


def _reads_only(name, args):
    """T-4154: a command beside `curl … | python3 -c …` that only reads, so no module python could import is planted:
    a _READS program without the flags that make it write or run something (uniq IN OUT writes OUT), git's read
    subcommands without --output/--ext-diff/--textconv, or a Sampo verb of _FM_PRINTS with plain words, --json and
    -p PROJECT. The same lists read_only() and _keeps_folders() trust."""
    if name == "git":
        return args[:1] != [] and args[0] in _GIT_READS and not _bad_flag(args[1:], _GIT_BAD)
    if name in ("sampo", "fm"):
        words, i = [], 0
        while i < len(args):
            if args[i] in ("-p", "--project") and i + 1 < len(args):
                i += 2  # its value is the project, not a verb word
                continue
            if args[i].startswith("-") and args[i] != "--json":
                return False
            if not args[i].startswith("-"):
                words.append(args[i])
            i += 1
        # the whole reader form: the verb words, then only the id a show takes (`inbox swipe`, `queue X` are others)
        return any(tuple(words[:len(v)]) == v and (len(words) == len(v) or v == ("task", "show") and len(words) ==
                                                   len(v) + 1 and re.fullmatch(r"T-\d+", words[-1]) is not None)
                   for v in _FM_PRINTS)
    bad = _READS.get(name)
    if bad is None or _bad_flag(args, bad):
        return False
    return not (name == "uniq" and len([w for w in args if w == "-" or not w.startswith("-")]) > 1)  # a bare - is stdin, an operand


def _writes_nothing(x, cwds):
    """T-0344 review: a command beside the python that can't create a file python would then import: no output
    redirect, and curl or wget only with flags that keep their download on stdout (any other flag, -o/-O, a config
    or a cookie jar, voids it); every other data tool writes nowhere but stdout. T-0414: no word with a substitution
    left in it (a quoted "$( )" or `…` runs a command _split doesn't see; an unquoted $( ) is a command of its own,
    checked here too) and no printf -v; a command of NAME=value words only sets names, each absent from the
    environment (so not exported to python) and none that python, its loader or the shell reads (_REACHES_PYTHON).
    T-3693: a tool given as a path must be that tool by the file it runs too (./cat linked to curl takes -o)."""
    if x.redirs or any(_RUNS.search(w) or _BRACES.search(w) for w in x.argv):  # T-0422: {-o,json.py} is -o json.py
        return False
    sets = [_ASSIGN.match(w) for w in x.argv]
    if x.argv and all(sets):
        return not any(m.group(1) in os.environ or _REACHES_PYTHON.fullmatch(m.group(1)) for m in sets)
    a, _ = _strip_wrappers(x.argv)
    real = _system_files(a[0], cwds) if a else None
    if x.argv != a or real is None or os.path.basename(a[0]) not in _DATA_TOOLS | _READ_ONLY_NAMES or any(
            os.path.basename(p) != os.path.basename(a[0]) for p in real):
        return False
    name, args = os.path.basename(a[0]), a[1:]
    if name not in _DATA_TOOLS:  # T-4154: a reader that is no download tool (ls, git log, sampo task show) writes no file
        return _reads_only(name, args)
    if name == "printf" and _setter(name, args):
        return False
    if name == "sed":
        return _sed_prints(args)
    if name == "docker":
        return _docker_reads(x, args, cwds)
    if name == "curl":  # value flags: a header or agent, a -w format (not %output{file}), -o only to /dev/null; T-3683:
        i = 0  # -b reads cookies (a string or a jar it only reads; -c is the jar it writes)
        while i < len(args):
            w, v = args[i], args[i + 1] if i + 1 < len(args) else ""
            if w in ("-H", "--header", "-A", "--user-agent", "-X", "--request", "-b", "--cookie") or \
                    w in ("-w", "--write-out") and "%output{" not in v or w in ("-o", "--output") and v == "/dev/null" \
                    or _CURL_TIMES.fullmatch(w) and re.fullmatch(r"\d+(?:\.\d+)?", v):
                i += 2
            elif w.startswith("-") and not _CURL_SAFE.fullmatch(w):
                return False
            else:
                i += 1
        return True
    if name == "nvidia-smi":  # T-0397: -f/--filename logs to a file; every other option prints
        return not any(w == "-f" or (w.startswith("-f") and not w.startswith("--")) or w.startswith("--filename")
                       for w in args)
    if name == "wget":  # every -O must name stdout (the last one wins: -qO- URL -O json.py writes json.py)
        outs, i = [], 0
        while i < len(args):
            w = args[i]
            if w in ("-O", "-qO", "--output-document"):
                outs.append(args[i + 1] if i + 1 < len(args) else "")
                i += 2
                continue
            if w in ("-O-", "-qO-") or w.startswith("--output-document="):
                outs.append("-" if w in ("-O-", "-qO-") else w.partition("=")[2])
            elif w.startswith("-") and w != "-" and w not in _WGET_SAFE:
                return False
            i += 1
        return bool(outs) and all(o == "-" for o in outs)
    return True


def _sed_prints(args):
    """T-3807: sed with print-only flags (no -i, no -f script file) and every script a ;-list of _SED_SAFE commands;
    the files after it are read only."""
    scripts, i, named = [], 0, False
    while i < len(args):
        w = args[i]
        if w in ("-e", "--expression"):
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            named, i = True, i + 2
            continue
        if w.startswith("--expression="):
            scripts.append(w.partition("=")[2])
            named = True
        elif w.startswith("-") and w != "-":
            if w not in _SED_FLAGS and not re.fullmatch(r"-[nErsuz]+", w):
                return False
        elif not named and not scripts:
            scripts.append(w)  # the first operand is the script; the rest are files
        i += 1
    return bool(scripts) and all(all(_SED_SAFE.fullmatch(part) for part in s.split(";")) for s in scripts)


def _docker_reads(x, args, cwds):
    """T-3807: docker port, ps, inspect, logs, images, version, info or top, which print and write no host file; or
    docker exec into a container running a data tool that writes nothing there either (a bind mount can reach the
    host, so the inner command is judged as if it ran here)."""
    if not args:
        return False
    if args[0] in _DOCKER_READS:
        return True
    if args[0] != "exec":
        return False
    i = 1
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-u", "--user", "-w", "--workdir", "-e", "--env"):
            i += 2
        elif re.fullmatch(r"-[itd]+|--interactive|--tty|--detach|--(?:user|workdir|env)=.*", args[i]):
            i += 1
        else:
            return False
    return i + 1 < len(args) and _writes_nothing(Cmd(args[i + 1:], [], x.piped), cwds)


def _shadowed(cwds, mods):
    """T-0344 review: a module python -c would import from the folder it runs in instead of the standard library — a
    name the code imports, or any stdlib name (json imports re, which imports enum) — as X.py, X.pyc, an extension
    module or a package (X/__init__.py); a plain folder shadows nothing."""
    names = set(sys.stdlib_module_names) | set(mods)
    for b in cwds:
        try:
            entries = os.listdir(b)
        except OSError:
            return True  # a folder the guard can't read could hold anything
        for n in entries:
            stem, _, ext = n.partition(".")
            if stem in names and (ext.split(".")[-1] in ("py", "pyc", "so", "pyd") and ext or
                                  os.path.isfile(os.path.join(b, n, "__init__.py"))):
                return True
    return False


def _py_code(args):
    """T-3683: (the flags before -c, CODE) when python's words are a run of flags that only narrow what it reads or
    does, then -c CODE; else None. -I (isolated: -E, -s and -P at once), -E (no PYTHON* variable), -s, -S, -P (no
    script folder on sys.path; a Python older than 3.11 rejects it and runs nothing), -B, -u, -q, -O, -OO, and -X
    importtime or utf8, apart or fused (-IB, -IBc CODE). Never -i (stdin runs as code after CODE), -m, -W (imports
    a warning category), another -X (pycache_prefix reads planted .pyc), --, - or a script; -Icexec(input()) is
    CODE fused to -c, so it isn't proved."""
    flags, i = "", 0
    while i < len(args):
        w = args[i]
        if w in ("-Ximporttime", "-Xutf8") or w == "-X" and args[i + 1:i + 2] in (["importtime"], ["utf8"]):
            i += 2 if w == "-X" else 1
            continue
        m = re.fullmatch(r"-([IEsSPBuqO]*)(c?)", w)
        if not m or w == "-":
            return None
        flags += m.group(1)
        if m.group(2):
            return (flags, args[i + 1]) if i + 1 < len(args) else None
        i += 1
    return None


def _unloop(x):
    """T-3683: a loop's words read as its unrolled body would: `for NAME in WORDS` sets NAME to each word in turn, a
    leading do, while or until goes (the command after it runs as is), and a lone done runs nothing (its redirects
    still count). Any other form (for NAME without in, for ((…)), select, a { group) is x itself, which
    _writes_nothing still reads as unknown."""
    a = x.argv
    if len(a) >= 4 and a[0] == "for" and re.fullmatch(r"[A-Za-z_]\w*", a[1]) and a[2] == "in":
        a = [f"{a[1]}={w}" for w in a[3:]]
    else:
        while a and a[0] in ("do", "while", "until"):
            a = a[1:]
        a = [] if a == ["done"] else a
    return x if a is x.argv else Cmd(a or ["true"], x.redirs, x.piped, x.procsub, x.op, x.depth)


def _reads_pipe_as_data(c, cmds, cwds, cmd, ignore_writes=False):
    """T-0344: `curl … | python3 -c CODE` reads the download as data when CODE is proved (_open_targets: pure modules,
    nothing dynamic, nothing written) and nothing makes python read stdin as code. -c comes first, after narrowing
    flags only (every word after CODE is sys.argv, so no -i before it); python runs bare and every other command is a plain data tool that writes no
    file or a plain assignment of a name python can't see (_writes_nothing), so no env, export or eval can set
    PYTHONINSPECT (its review: PYTH""ONINSPECT=1) and no module can appear before python starts; none is inherited; and
    nothing in the folder it runs in shadows a module it may import (_shadowed: python -c searches that folder first).
    T-0414: "every other" is every command up to the end of python's pipeline when python sits outside every ( ) (a
    group's redirect or pipe is written after python but opens before it or runs beside it); what runs after python
    exits can't change it. A bare & refuses outright (it runs a command beside python, and `&</dev/null x` hides x
    from _split), as does a substitution in a live heredoc body. A name set here may be read only where its every value
    is a plain word: no space, glob, quote, $ or leading -, so it can't split into flags or close a string in CODE.
    T-3683: flags that only narrow python may come before -c (_py_code): -I or -P keeps the folder off sys.path (no
    _shadowed), -I or -E ignores the inherited PYTHON* variables. A loop reads as its unrolled body (_unloop): every
    command on the line counts, since later passes of python run after the whole body. ignore_writes skips only the
    writers outside python's own pipeline, which running that pipeline alone leaves behind. Another python on the line
    that passes the same proof (_proved_py) writes nothing either: a literal loop unrolls into one per word."""
    if not _proved_py(c, cwds):
        return False
    shell = _mask_quotes(_strip_heredocs(cmd))
    if re.search(r"(?<![&<>|])&(?![&>])", shell) or any(_RUNS.search(b) for b in _live_heredocs(cmd)):
        return False
    i = start = end = cmds.index(c)
    while end + 1 < len(cmds) and "|" in cmds[end + 1].op.replace("||", ""):
        end += 1
    while start > 0 and "|" in cmds[start].op.replace("||", ""):
        start -= 1
    # review: _split counts a quoted ")" as a real one (shlex makes it a bare token), so python can look outside a
    # group it is in; bare paren tokens that outnumber the unquoted parens mean the depths can't be trusted
    bare = [t for t in _tokens(_lines(_strip_heredocs(cmd))) if re.fullmatch(r"[$<>]?\(+|\)+", t)]
    top = (c.depth == 0 and all(x.depth >= 0 for x in cmds) and shell.count("(") == shell.count(")")
           and sum(t.count("(") for t in bare) == shell.count("(") and sum(t.count(")") for t in bare) == shell.count(")"))
    loop = any(_unloop(x) is not x for x in cmds)
    rest = [x for x in (cmds[:end + 1] if top and not loop else cmds) if x is not c]
    others = [_unloop(x) for x in rest]
    if any(not _writes_nothing(u, cwds) and not _proved_py(u, cwds) for x, u in zip(rest, others)
           if not ignore_writes or x in cmds[start:end + 1]):
        return False  # ignore_writes: T-3673, to tell why it said no
    pairs = [w.split("=", 1) for x in others if all(_ASSIGN.match(w) for w in x.argv) for w in x.argv]
    loose = {k for k, v in pairs if v.startswith("-") or not re.fullmatch(r"[\w.:/@%+,=-]*", v)}
    words = [w for x in others + [c] if not all(_ASSIGN.match(v) for v in x.argv) for w in x.argv]
    read = {r for w in words for r in re.findall(r"\$\{?[#!]?([A-Za-z_]\w*)", w)}
    return not (loose & read or loose and any("${!" in w for w in words))  # ${!R} reads the name R holds


_PY3 = lazy(r"python3(?:\.\d+)?")


def _proved_py(x, cwds):
    """T-0344: x is a bare python -c CODE (no wrapper or assignment, output to /dev/null at most) that reads stdin
    only as data and writes nothing: CODE proved (_open_targets), no inherited PYTHONINSPECT or import path, nothing
    in its folder shadowing a module it may import (_shadowed). T-3683: narrowing flags first (_py_code); -I or -E
    ignores the inherited PYTHON* variables, -I or -P keeps the folder off sys.path. T-3689: only a python named for
    major version 3 (python3, python3.N): Python 2's input() evaluates stdin as code, and plain python may be either
    (the hook's PATH isn't the Bash tool's, so the guard can't tell which, and it never runs a program to ask).
    T-3693: a python given as a path must also be one by the file it runs (_system_files: ./python3 linked to bash), a
    file only root can change. T-3714: then its own name may be plain python (a venv's bin/python links to python3.N)."""
    argv, _ = _strip_wrappers(x.argv)
    real = _system_files(argv[0], cwds) if argv else None
    py3 = real is not None and x.argv == argv and all(_PY3.fullmatch(os.path.basename(w)) for w in real or argv[:1])
    py = _py_code(argv[1:]) if py3 else None
    if not py or not set(x.redirs) <= {"/dev/null"}:
        return False
    flags, code = py
    if not set(flags) & set("IE") and any(os.environ.get(k) for k in ("PYTHONINSPECT", *_PY_PATH_VARS)) or \
            _open_targets(code) != []:
        return False
    if set(flags) & set("IP"):
        return True
    import ast
    mods = {al.name.split(".")[0] for n in ast.walk(ast.parse(code)) if isinstance(n, ast.Import) for al in n.names}
    return not _shadowed(cwds, mods)


def _real_files(word, cwds):
    """T-3693: the real files a command word given as a path runs, one per folder the command may run in (realpath:
    ./python3 linked to bash runs bash); [] for a bare name (the guard never searches PATH: the hook's isn't the Bash
    tool's); None when the word is computed or names no file in some folder (it may be made earlier on the line)."""
    if "/" not in word:
        return []
    out = []
    for b in [] if re.search(r"[$`*?\[~\\]", word) else [""] if word.startswith("/") else cwds:
        p = os.path.realpath(os.path.join(b, word))
        # T-3714's case: the path as written must reach a file too; realpath folds v3/bin/nope/../python to a file
        # that exists, though bash finds no nope (or one a link made earlier on the line points elsewhere)
        if not (os.path.isfile(p) and os.path.isfile(os.path.join(b, word))):
            return None
        out.append(p)
    return out or None


def _system_files(word, cwds):
    """T-3693: _real_files when each is a file only root can change, as a name proves nothing about a file the user
    owns (a copy of bash named python3); None otherwise, and for every path when the guard itself runs as root."""
    real = _real_files(word, cwds)
    for p in real or []:
        try:
            st = os.stat(p)
        except OSError:
            return None
        if st.st_uid != 0 or st.st_mode & 0o022 or os.geteuid() == 0:
            return None
    return real


def _a_path(word, fixed):
    """T-0331: a command word that runs a file, never a builtin or function: a literal / in it, and its only
    expansions $NAME or ${NAME} of plain literals this command set before it (fixed: no space or glob, so they can't
    split into `eval …`; its review). `$S/app` with S=/tmp/x is a path; `$S/etc` with S unknown may be `eval X=/etc`."""
    rest = re.sub(r"\$(?:\{\w+\}|\w+)", "", word)
    return "/" in rest and not re.search(r"[$`*?\[\\'\"\s]", rest) and all(
        (a or b) in fixed for a, b in re.findall(r"\$(?:\{(\w+)\}|(\w+))", word))


def _outside(shell, cmds, ctx, fixed=None):
    """T-0175 review: ({NAME: value} for the names this command can't set itself, as it inherits them; the names it may
    set). It may set every name it writes bare (an assignment, read NAME, for NAME, ${NAME:=…}), and any name at all
    (None) with a builtin that sets names indirectly (eval, read, declare, source…) or a computed command name. Values
    are Claude Code's environment, HOME the user's; one a shell would split or expand stays unknown."""
    for c in cmds:
        argv, _ = _strip_wrappers(c.argv)
        if argv and (_setter(os.path.basename(argv[0]), argv[1:]) or re.search(r"[$`*?\[]", argv[0]) and not _a_path(
                argv[0], fixed or {})):
            return {}, None
    bare = set(re.findall(r"[A-Za-z_]\w*", re.sub(r"\$\w+|\$\{\w+\}", " ", shell)))
    return {k: v for k, v in {**os.environ, "HOME": ctx.home}.items() if k not in bare and not _SHELL_SET.match(k)
            and v and not re.search(r"[\s*?\[]", v) and not v.startswith("~")}, bare


def _cd_from(tgt, here, ctx):
    """T-1727 review: where `cd tgt` from here lands, for recording discards; None when the guard can't tell (an
    unresolved name, $OLDPWD, a relative cd from an unknown folder)."""
    if _unresolvable(tgt):
        return None
    t = _expand(tgt, ctx)
    return os.path.normpath(t) if os.path.isabs(t) else _resolve(t, here) if here else None


def _cd_real(tgt, here, ctx):
    """T-4673: where `cd tgt` from here really lands when a '..' comes after a link ("" when there is none or the guard
    can't tell). _cd_from collapses `cd link/..` to the link's folder, as bash's default cd -L does; cd -P and set -P
    land in the link target's parent, where a later relative name is another file. A cd with no '..' pays nothing."""
    if not here or ".." not in tgt or _unresolvable(tgt):
        return ""
    return _walk(_expand(tgt, ctx), here)[1]


_CD_TARGET = lazy(r"""(?:^|[\s;&|(])(?:cd|pushd)\s+(?:-[LPe@]+\s+|--\s+)*(["']?)([^\s;&|"'<>()]+)\1""")


def _climb_dirs(text, ctx):
    """T-4673: the folders a `cd T` in text may land in when T has a '..' after a link, both readings (where bash's
    cd -L collapses it, and where cd -P goes), for the code that names a relative file after such a cd; [] when no cd
    in text climbs through a link. Only a text that has a '..' pays for the scan."""
    if ".." not in text:
        return []
    out = []
    for m in _CD_TARGET.finditer(text):
        t = m.group(2)
        if ".." in t and not _unresolvable(t):
            path, real = _walk(_expand(t, ctx), ctx.cwd)
            if real and os.path.isdir(real):
                out += [path, real]
    return list(dict.fromkeys(out))


def _moved(cwds, lost, tgt, sure, cdpath, ctx):
    """T-0175 review: the places the shell may be after a cd: cwds, the ones the guard can name, and lost, the paths
    below a folder it can't name (a cd to a target it can't resolve, or a relative one CDPATH may send elsewhere). When
    the cd surely happened (sure: an && chain stops at a failed one; a straight command's cd into an existing folder
    can't fail) the new places replace the old; otherwise it may fail or run in a subshell, so they add to them."""
    if _unresolvable(tgt):
        new, tails = [], [""]
    else:
        t = _expand(tgt, ctx)
        if os.path.isabs(t):
            new, tails = [os.path.normpath(t)], []
            new += [r for r in (_walk(t, "/")[1],) if r]  # T-4673: cd -P lands where a '..' after a link goes
        else:
            new = [_resolve(t, b) for b in cwds]
            new += [r for b in cwds for r in (_walk(t, b)[1],) if r]
            tails = [os.path.join(x, t) for x in lost] + ([t] if cdpath else [])
    return list(dict.fromkeys(new if sure else cwds + new)), list(dict.fromkeys(tails if sure else lost + tails))


_BRACE = lazy(r"\{([^{}]*)\}")
_SEQ = lazy(r"(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])(?:\.\.-?\d+)?")
EXPANSIONS_MAX = 1024  # ponytail: a brace product past this is checked only this far


def _expansions(path):
    """T-0177: the paths bash may make of a target: comma and sequence braces (innermost first; {x} with neither stays
    as written) and, for any with * ? or [, the existing paths it matches."""
    out, todo = [], [path]
    while todo and len(out) + len(todo) <= EXPANSIONS_MAX:
        p = todo.pop()
        m = next((m for m in _BRACE.finditer(p) if "," in m.group(1) or _SEQ.fullmatch(m.group(1))), None)
        if not m:
            out.append(p)
            continue
        seq, inner = _SEQ.fullmatch(m.group(1)), m.group(1)
        if seq:
            a, b = seq.group(1), seq.group(2)
            num = a.lstrip("-").isdigit() and b.lstrip("-").isdigit()
            lo, hi = (int(a), int(b)) if num else (ord(a), ord(b))
            step = 1 if hi >= lo else -1
            alts = [str(i) if num else chr(i) for i in range(lo, hi + step, step)][:EXPANSIONS_MAX]
        else:
            alts = inner.split(",")
        todo += [p[:m.start()] + x + p[m.end():] for x in alts]
    out += todo
    wild = [p for p in out if re.search(r"[*?\[]", p)]
    if not wild:  # T-3577: glob (and its contextlib) only for a pattern
        return list(dict.fromkeys(out))
    import glob
    import itertools
    return list(dict.fromkeys(out + [g for p in wild for g in itertools.islice(glob.iglob(p), EXPANSIONS_MAX)]))


def _makeable(d):
    """mkdir -p d can succeed: its nearest existing folder is a folder this user may write and enter."""
    while not os.path.exists(d):
        d = os.path.dirname(d)
    return os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK)


def _possible(path):
    """False when a folder in the path is an existing file: nothing can be written there."""
    d = os.path.dirname(path)
    while d not in ("", "/"):
        if os.path.exists(d):
            return os.path.isdir(d)
        d = os.path.dirname(d)
    return True


GUESS_WORDS = 48  # ponytail: words tried per unknown part (two parts: their pairs); a longer command is guessed this far
_NO_GUESS = []  # T-1778: non-empty while a shell script's pieces are read (_check_script keeps no guessed target)


_PLAIN = r"[^()`'\"\\#\n]*"  # no quote, escape, comment, newline or paren: bash's closing ) is the first one


def _mask_substs(shell):
    """T-0370: each plain $(…) and `…` (innermost first, so nesting unwinds) as $___…, an unknown value of the same
    length; a tail's leading plain `…)` (the rest of a substitution it opens in) too. Only plain bodies: a quote, \\),
    a # comment, a heredoc or a case pattern can move the real closing paren past the first, and an S= inside it would
    then look like this shell's (its review: X=$(echo \\); S=/tmp/ok; true); rm -rf $S/ is rm -rf /)."""
    def mask(m):
        body = m.group(0)
        return body if re.search(r"\bcase\b|<<", body) else "$" + "_" * (len(body) - 1)
    shell = re.sub(r"^" + _PLAIN + r"\)", lambda m: m.group(0) if mask(m) == m.group(0) else " " * len(m.group(0)),
                   shell)  # a tail's head ran in the subshell: no command of this shell
    while True:
        t = re.sub(r"\$\(" + _PLAIN + r"\)|`[^()`'\"\\#\n]*`", mask, shell)
        if t == shell:
            return shell
        shell = t


_PURE = {}  # text -> _masked(text): bounded, never stale (the reading looks at the text alone)
_FS_MEMO = {}  # what a reading asked the file system or Sampo's own layout (cleared when a top-level reading starts)


def _mask_quotes(text):
    """T-0384: the text inside '…' and "…" as underscores, same length, the quotes kept: a quoted ( | ; & or keyword
    is a literal, never a branch (a jq filter or sed script made the whole line read as branchy). A # comment is
    blanked to its line's end too (its review: a quote in a comment paired with one in a later comment hid a ||).
    Escapes and comments as _strip_comments reads them; unchanged (fails closed) under $'…', an unclosed quote or a
    backtick or ${…} operator inside double quotes, whose own quotes don't pair with the outer ones. T-3799: a $( … )
    inside double quotes is code: kept, with its own quotes masked the same way (unpaired: unchanged, as before)."""
    got = _pure(text, _masked)  # T-4758: the same text is masked by a dozen readers, and by each tail of a script's
    return text if got is None else got  # pieces (a pure function of its text: the cost went with the square of its size)


_PURE_CHARS = [0]


def _pure(text, fn, kind=""):
    """fn(text), remembered: only for a function of its text alone (never stale), the memory bounded by the characters
    held (a script's prefixes are long) as well as the entries."""
    key = (kind, text)
    try:
        return _PURE[key]
    except KeyError:
        pass
    if _PURE_CHARS[0] > 4_000_000 or len(_PURE) > 20000:
        _PURE.clear()
        _PURE_CHARS[0] = 0
    _PURE_CHARS[0] += len(text) + 1
    got = _PURE[key] = fn(text)
    return got


def _subst_end(text, k):
    """T-3799: the index just past the ) closing the $( whose body starts at k, read with its own quotes, escapes and
    nested parentheses; None when nothing closes it, or (its review) where a ) or quote may not be what it looks
    like: a case pattern's ), a heredoc body, a # comment (_subst_bodies reads those; this reading stays simple)."""
    depth, q, i = 1, None, k
    while i < len(text):
        ch = text[i]
        word = i == k or not re.match(r"[\w$-]", text[i - 1])  # a word starts here
        if q is None and (text.startswith("<<", i) or ch == "#" and (i == k or text[i - 1] in " \t\n;&|(")
                          or word and re.match(r"case(?![\w-])", text[i:])):
            return None
        if ch == "\\" and q != "'":
            i += 2
            continue
        if q == '"' and text.startswith("$(", i):
            j = _subst_end(text, i + 2)
            if j is None:
                return None
            i = j
            continue
        if q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if not depth:
                return i + 1
        i += 1
    return None


def _masked(text):
    """_mask_quotes' reading, or None where it fails closed."""
    out, q, i = [], None, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and q != "'":
            out.append(text[i:i + 2] if q is None else "_" * len(text[i:i + 2]))
            i += 2
            continue
        if q is None and text.startswith("$'", i):  # T-1473: only a real $'…' (unescaped, unquoted) fails closed
            return None
        if q == '"' and text.startswith("$(", i):  # T-3799: code the quotes run, its own quotes masked
            j = _subst_end(text, i + 2)
            inner = _masked(text[i + 2:j - 1]) if j else None
            if inner is None:
                return None
            out.append("$(" + inner + ")")
            i = j
            continue
        if q == '"' and (ch == "`" or (text.startswith("${", i) and not re.match(r"\$\{\w+\}", text[i:]))):
            return None
        if q is None and ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|(<>"):
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            out.append("_" * (j - i))
            i = j
            continue
        if q and ch != q:
            out.append("_")
        else:
            q = None if q else (ch if ch in "'\"" else None)
            out.append(ch)
        i += 1
    return None if q else "".join(out)


# `done` ends the loop only where a command starts (`echo done` is an argument)
_LOOP = lazy(r"(?<![\w$-])for\s+([A-Za-z_]\w*)\s+in\s+([^;\n]*?)\s*[;\n]\s*do\b(.*?[;\n&])\s*done(?![^\s;&|()<>])",
                   re.S)
_LITERAL_WORD = lazy(r"[\w./@:+,=%-]+")
_LOOP_SETTERS = lazy(r"(?<![\w-])(?:read|readarray|mapfile|declare|typeset|local|export|printf|eval|unset|let|"
                           r"source|for|while|until|select|case|function|do|done|break|continue)(?![\w-])|"
                           r"(?:^|[\s;&|(])\.\s|<<|\$\{!")  # break/continue: V may keep an earlier word


def _single_quoted(text, i):
    """Whether position i of text is inside '…' as bash reads it (escapes as _mask_quotes reads them)."""
    q, j = None, 0
    while j < i:
        ch = text[j]
        if ch == "\\" and q != "'":
            j += 2
            continue
        if q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        j += 1
    return q == "'"


def _escapes(cmd):
    """T-1781: whether cmd holds a backslash bash may read as an escape: any outside '…' (in "…" too). A quote inside
    "…" opens nothing; text this scan can't follow ($( … ) quoting inside "…") counts as an escape."""
    q = None
    for ch in cmd:
        if q == "'":
            q = None if ch == "'" else q
        elif ch == "\\":
            return True
        elif ch == '"':
            q = None if q == '"' else '"'
        elif ch == "'" and q is None:
            q = "'"
    return False


_SAMPO_IN_BODY = lazy(r"(?<![\w.-])(?:sampo|fm)(?![\w-])|/sampo(?![\w-])")


def _expand_literal_loops(cmd):
    """T-0411: a top-level `for V in w1 w2; do BODY; done` whose words are plain literals runs BODY once per word, so
    it is read as `BODY[w1]; BODY[w2]; V=w2`. Only when the body has no nested loop, case, heredoc or way to set V
    (an assignment, read, eval, source …), and no $V bash leaves unexpanded ('…'); break and continue only make bash
    do less than what is checked. Anything else is left as written (V stays unknown, as before)."""
    # IFS: bash splits the word it substitutes; a backslash: an escaped ; or & separates nothing (T-1781: one inside
    # '…' is a character, so `echo 'a\'` later in the command no longer leaves every loop unread)
    if "for" not in cmd or "$'" in cmd or "<<" in cmd or "IFS" in cmd or _escapes(cmd):
        return cmd
    masked = _mask_quotes(cmd)
    if masked == cmd and re.search(r"['\"]", cmd):  # quoting the guard can't read: leave it
        return cmd
    out, last = [], 0
    for m in _LOOP.finditer(masked):
        var, words = m.group(1), cmd[m.start(2):m.end(2)].split()
        body, mbody = cmd[m.start(3):m.end(3)], m.group(3)
        before = masked[:m.start()].rstrip()
        uses = list(re.finditer(rf"\$(?:\{{{var}\}}|{var}(?!\w))", body))
        # T-3683: V set where bash sets it matters when a command run in the body reads it (PATH picks the program):
        # such a V, or an exported one, stays as written
        if (before and before[-1] not in ";&|\n(" or not words or not all(_LITERAL_WORD.fullmatch(w) for w in words)
                or var in os.environ or _REACHES_PYTHON.fullmatch(var) or _LOOP_SETTERS.search(mbody) or re.search(rf"(?<![\w$-]){var}(?!\w)", re.sub(
                    rf"\$(?:\{{{var}\}}|{var}(?!\w))", "", body)) or any(_single_quoted(body, u.start()) for u in uses)):
            continue
        after = masked[m.end():].lstrip(" \t")
        # T-4785 round 8: a loop that runs a sampo word is unrolled only where it is plainly top level in this shell:
        # nothing before it but a separator (no `(`, `&&`/`||` branch, pipe, `&`) and no pipe or `&` after it
        # (`a && for …` stays: the commands are the text, and V is not kept behind a branch)
        if _SAMPO_IN_BODY.search(body) and (before and before[-1] not in ";\n" and before[-2:] not in ("&&", "||")
                                            or after[:1] == "|" and after[:2] != "||"
                                            or after[:1] == "&" and after[:2] != "&&"):
            continue
        runs ="; ".join(re.sub(rf"\$(?:\{{{var}\}}|{var}(?!\w))", lambda _, w=w: w, body).strip().rstrip(";").strip()
                         for w in words)
        # V keeps the last word only when the loop surely ran in this shell: not behind && / || (maybe skipped) or in
        # a pipeline (a subshell)
        ran = not before or before[-1] in ";\n(" or before[-1] == "&" and before[-2:] not in ("&&", "|&")
        keeps = ran and (not after or after[0] in ";\n" or after[:2] in ("&&", "||"))
        out.append(cmd[last:m.start()] + runs + (f"; {var}={words[-1]}" if keeps else ""))
        last = m.end()
    return "".join(out) + cmd[last:] if out else cmd


_MOVERS = lazy(r"(?<![\w./-])(?:cd|pushd|popd|eval|source|trap|mapfile|readarray)(?![\w./-])|(?:^|[\s;&|(])\.\s")
_CD_TO = lazy(r"(?<![\w./-])(?:cd|pushd)\s+(?:-[LPe@]+\s+)*([^\s;&|<>()'\"`$]+)")
# a command word bash computes: a $ expansion or a quote right where a command starts
_COMPUTED = lazy(r"(?:^|[;&|(\n]|\b(?:then|do|else|elif|time|!))\s*[\"']?\$")


def _cd_targets(code):
    """T-0384: the literal folders shell text's cd and pushd lines name, quotes and backslashes removed as bash does."""
    code = _strip_comments(code)
    return list(dict.fromkeys(_CD_TO.findall(code) + _CD_TO.findall(re.sub(r"[\\'\"]", "", code))))


_PATTERN_WORD = r"[\w.*?\[\]/:=+@%,-]+"
_CASE_ARM = lazy(rf"((?:\bin|;;&?|;&)[ \t]*\n(?:[ \t]*(?:#.*)?\n)*)[ \t]*\(?{_PATTERN_WORD}(?:[ \t]*\|[ \t]*{_PATTERN_WORD})*[ \t]*\)")
_FRAME_WORD = lazy(r"(?<![^\s;&|(])(?:\{|\}|\[\[?)(?![^\s;&|)])")  # a lone { } [ [[ : a group, a function body, a test
_PARAM_SIMPLE = lazy(r"\$\{[#!]?\w+(?:(?::?[-+=?]|[#%/:]{1,2})[^{}`'\"$\n]*)?\}|\$\(\([^()]*\)\)")  # ${x:-y}, $((1 + 2))


def _frame_free(code):
    """T-4811: shell text without what only frames a script: a case arm's pattern line (`*)`, `a|b)`, after `in` or `;;`),
    a lone { or } (group, function body), [ or [[ (the test, read as `test`), a ${name:-word} and a $(( … )): none of them
    names, computes or moves a command, and every long script holds dozens, each of which made the text read as one
    that may move (a glob, a brace, a substitution) and kept every `$( … )` tail of it for a second reading."""
    code = _CASE_ARM.sub(r"\1", code)
    code = _FRAME_WORD.sub(lambda m: " test " if m.group().startswith("[") else " ", code)
    return _PARAM_SIMPLE.sub("V", code)


def _moves_text(code, frames=False):
    """T-0384: whether shell text may cd this shell: a mover word, also once quotes and backslashes are removed (c\\d
    and c""d are cd), a command word it computes, or a brace, glob or substitution that may make one. frames: the glob
    and brace test skips what only frames a script (_frame_free); the words that move or compute still count."""
    code = _strip_comments(code)
    free = _frame_free(code) if frames else code
    return bool(_MOVERS.search(code) or _MOVERS.search(re.sub(r"[\\'\"]", "", code)) or _COMPUTED.search(code)
                or re.search(r"[`{*?\[]|\$\(", free))


def _moves_shell(name, args, cwds, ctx, shell=""):
    """T-0384: (whether eval, source, ., trap or mapfile -C may run a cd in this shell or one more of themselves, the
    literal folders its cd and pushd lines name). Computed text, a script found on PATH, one that isn't a plain file
    (its review: /dev/stdin, a FIFO), one the command also names elsewhere (it may write it first), one given
    arguments, or one it can't read may move it; a literal or a readable script with none of those doesn't."""
    if name in ("mapfile", "readarray"):
        cbs = [args[j + 1] for j, a in enumerate(args[:-1]) if a == "-C"] + [a[2:] for a in args if a.startswith("-C")
                                                                              and len(a) > 2]
        return (bool(cbs) and any(_moves_text(cb) or not cb for cb in cbs)), [d for cb in cbs for d in _cd_targets(cb)]
    if name in ("eval", "trap"):
        rest = args[1:] if args[:1] == ["--"] else args
        if name == "trap" and rest[:1] and rest[0].startswith("-"):
            return rest[0] not in ("-p", "-l", "-P"), []  # -p and -l only print
        text = " ".join(rest) if name == "eval" else (rest[0] if rest else "")
        return bool(re.search(r"[$`\\]", text) or _moves_text(text)), _cd_targets(text)
    if len(args) != 1 or "/" not in args[0] or _unresolvable(args[0]):
        return True, []  # source NAME searches PATH first; arguments become its $1 …
    if shell.count(os.path.basename(args[0])) > 1:
        return True, []
    moves, places = False, []
    for base in cwds:
        path = _resolve(_expand(args[0], ctx), base)
        if not os.path.isfile(path) or re.match(r"/(?:dev|proc|sys)/", os.path.realpath(path)):
            return True, places
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                code = f.read(1_000_000)
        except OSError:
            return True, places
        if _moves_text(code):
            moves, places = True, places + _cd_targets(code)
    return moves, places


def _prefix_vars(shell, cmds, home=None, spaces=False):
    """T-0183: NAME=literal values a branchy command sets in its straight top-level prefix (the commands before its
    first branch, pipe, subshell, group or keyword run first, in this shell), for names written bare nowhere else in
    it; {} when nothing qualifies. Only where no builtin can set names indirectly (the caller checks)."""
    # T-0370: a substitution is no branch (a subshell sets nothing here): read the prefix with each one as an unknown
    # value of the same length, so `f=$(ls …); S=/tmp/x; … > $S/a` knows S; f stays unknown, and a name also set
    # inside a substitution is written twice (counts, below, reads the real text)
    full, shell = shell, _mask_substs(shell)
    s = _mask_quotes(re.sub(r"\$\{\w+\}|\d*>&\d*-?|&>>?", lambda m: " " * len(m.group(0)), shell))
    # T-0384: a pipeline is no cut either: its parts run in subshells and set nothing here (dropped below)
    cut = next((m.start() for m in re.finditer(r"&&|\|\||(?<!\|)\|&|&|[(){}`]|\$\(|\b(?:if|then|else|elif|fi|for|while|until|do|"
                                                r"done|case|esac|select|function|coproc)\b", s) if m.group(0) != "&&"), len(s))
    # what an && may skip is set for sure only if everything after it runs only when the chain got that far:
    # `false && HOME=/x; rm $HOME` runs the rm anyway (T-0338 review), `S=/x && rm $S/a && ls | head` doesn't
    amp = s.find("&&", 0, cut)
    skippable = amp >= 0 and re.search(r";|\n|\|\||(?<![&|])&(?!&)", s[amp + 2:].replace("&&", "  "))
    seps = list(re.finditer(r";|\n|&&", s[:cut]))
    if skippable:  # keep the elements that surely ran: after a ; or newline, or after && links that can't fail
        keep, start, sure = [], 0, True
        for m in seps:
            elem = shell[start:m.start()]
            if sure:
                keep.append(elem)
            sure = (sure and bool(re.fullmatch(r"\s*(?:\w+=[^\s;&|$`()<>'\"\\]*\s+)*\w+=[^\s;&|$`()<>'\"\\]*\s*",
                                                elem))) if m.group(0) == "&&" else True  # a plain assignment can't fail
            start = m.end()
        prefix = "; ".join(e.strip() for e in keep if e.strip())
    else:  # T-0330: a ; list mixed with && (D=~/x; mkdir -p $D && cd $D && tar xf -) isn't straight either
        prefix = shell[:seps[-1].start()] if seps and (cut < len(s) or not _straight_line(full) or full != shell) else ""
    # T-0384: drop the pipelines (no lastpipe without a shopt, a setter that voids all of this) and read the rest
    # with quoted text masked, as _raw_cmds reads quotes
    mp = _mask_quotes(prefix)
    if "$'" not in prefix and (mp != prefix or not re.search(r"['\"]", prefix)):  # unchanged with quotes: unread
        cuts = [0] + [x for m in re.finditer(r";|\n|&&", mp) if not (m.group(0) == "\n" and re.search(  # its review: a
            r"\|&?\s*$", mp[:m.start()])) for x in (m.start(), m.end())] + [len(prefix)]  # | ends no pipeline
        prefix = "; ".join(prefix[a:b].strip() for a, b in zip(cuts[::2], cuts[1::2])
                           if prefix[a:b].strip() and not re.search(r"(?<!\|)\|(?!\|)", mp[a:b]))
        mp = _mask_quotes(prefix)
    raw = _raw_cmds(prefix) if prefix and _straight_line(mp) else None
    env = {}
    for words in raw or []:
        env = _track_vars(words, env, home, spaces)
        if env is None:
            return {}
    # T-0331: a word after - / or . (a flag like -S, a path segment like /tmp/S/) can't name a variable
    # T-3803: nor a word right after = (tier=S, --x=S: a value sets no name), nor one inside quoted text (a sed
    # script's 'S = 2'); a quoted plain word (read "S") is read unquoted, as bash reads it, so it still counts
    counts = collections.Counter(re.findall(r"(?<![\w/.=-])[A-Za-z_]\w*", re.sub(r"\$\w+|\$\{\w+\}", " ", _mask_quotes(
        _PLAIN_QUOTED.sub(r"\2", full)))))

    def set_first(k):  # T-0370 review: `echo > $S/x; S=/tmp/x` writes /x (S is unset there): no value before it is set
        a, r = re.search(rf"(?<![\w$]){k}=", full), re.search(rf"\$\{{?{k}\b", full)
        return a is not None and (r is None or a.start() < r.start())
    return {k: v for k, v in env.items() if counts[k] == 1 and set_first(k)}


def _target_cats(target, known, bare, cwds, lost, shell, ctx, classify, note="", guess=True):
    """(category, detail) for one write target, through the variables known here (T-0175: a target with a $ in it was
    skipped), in every place the shell may be. A resolved one is named by its path, so a standing or trusted yes covers
    it as usual. One the command itself makes unknown (a name it sets in a way the guard can't follow, a command
    substitution) could be any path the command names, in the unknown part's place; a relative one after a cd the guard
    couldn't follow, any folder the command names. Each counts, the way interpreter code is judged."""
    here = cwds or [ctx.cwd]
    guess = guess and not _NO_GUESS
    t = re.sub(r"^~-(?=/|$)", "$OLDPWD", re.sub(r"^~\+(?=/|$)", "$PWD", target))  # review: ~+ and ~-
    t = _with_vars(t, {**known, "PWD": cwds[0]} if len(cwds) == 1 and not lost else known)
    found = []
    if not _unresolvable(t):
        e = _expand(t, ctx)
        for path, real in dict.fromkeys(_walk(e, b) for b in ([ctx.cwd] if os.path.isabs(e) else cwds)):
            found += _judge(classify, path, real, ctx, note)  # T-0180: its real path, as Edit's; T-4667: and a '..'s
            found += [(cat, f"{p}{note}") for p in _expansions(path) if p != path for cat in classify(p, ctx)]
        if os.path.isabs(e) or not lost or not guess:
            return found
    elif not guess:  # T-1224: reads and modes judge resolved paths only
        return found
    elif _only_unset(t, bare):
        # only names it neither sets nor inherits: bash expands them to nothing (T-0374: echo x > $NOPE/<guard file>
        # wrote the guard file), so the target is the rest of the word
        e = _expand(_PART.sub("", t), ctx)
        for path in dict.fromkeys(_resolve(e, b) for b in ([ctx.cwd] if os.path.isabs(e) else cwds)):
            found += [(cat, f"{target}{note} (an unset name is empty: {path})") for cat in classify(path, ctx)
                      if cat in _GUARDED_BY_PATH]
        return found
    raw = _line_words(shell, known)
    if _unresolvable(t):
        # T-0183: words as written, one per unknown part, the first two paired (a word in every part guessed
        # <abs>/<abs>), the rest a neutral name; the whole guess is resolved after. T-0374: and every part empty,
        # as an unset name (or a substitution that prints nothing) expands
        raw, pair = raw[:GUESS_WORDS], len(list(_PART.finditer(t))) > 1
        guesses = [_resolve(_expand(_PART.sub("", t), ctx), base) for base in here]
        for a in raw:
            for b in raw if pair else [None]:
                vals = iter((a, b))
                guess = _PART.sub(lambda _: next(vals, None) or "x", t)
                guesses += [_resolve(_expand(guess, ctx), base) for base in here]
    else:  # relative, somewhere a cd went that the guard couldn't follow: only a folder that exists takes a cd
        words = [p for w in raw for p in dict.fromkeys(_resolve(_expand(w, ctx), b) for b in here)]
        if not any(map(os.path.isabs, lost)):  # T-1930: w/x is a folder only below a folder w (each part must be
            words = [w for w in words if os.path.isdir(w)]  # one), so test w once, not once per lost x: 1M stats to 4K
        guesses = [os.path.join(w, x, _expand(t, ctx)) for w in words for x in lost if os.path.isdir(os.path.join(w, x))]
    for path in dict.fromkeys(guesses):
        if _possible(path):
            found += [(cat, f"{target}{note} (not known before it runs: it could be {path})")
                      for cat in classify(path, ctx) if cat in _GUARDED_BY_PATH]
    return found


def _raw_cmds(shell):
    """T-0161 review: a straight-line command's simple commands as written, quotes kept (shlex drops them, and
    `"D=x"`, `\\D=x` and `D"="x` are commands in bash, not assignments). None when the quoting can't be read."""
    if "$'" in shell:
        return None
    cmds, words, cur, q, i, text = [], [], "", None, 0, _strip_comments(shell)
    while i < len(text):
        ch = text[i]
        if ch == "\\" and q != "'":
            cur += text[i:i + 2]
            i += 2
            continue
        if q:
            q = None if ch == q else q
        elif ch in "'\"":
            q = ch
        elif ch in " \t\n;" or text[i:i + 2] == "&&":
            words, cur = (words + [cur]) if cur else words, ""
            if ch != " " and ch != "\t" and words:
                cmds, words = cmds + [words], []
            i += 2 if ch == "&" else 1
            continue
        cur += ch
        i += 1
    if q:
        return None
    words += [cur] if cur else []
    return cmds + ([words] if words else [])


def _unquote(word):
    try:
        return "".join(shlex.split(word))
    except ValueError:
        return None


def _shell_c(args):
    """The command strings `sh -c` runs: the word after a bare -c, and (T-0324) the first operand once any option
    cluster holds c (bash -lc, zsh -o x -ic, bash --rcfile f -ec -- CMD)."""
    return [args[i] for i in _shell_c_at(args)]


def _shell_c_at(args):
    """The places in args of the command strings `sh -c` runs (see _shell_c); what follows each is the script's argv."""
    out = [args.index("-c") + 1] if "-c" in args and args.index("-c") + 1 < len(args) else []
    has_c, i = False, 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
        elif re.fullmatch(r"[-+][a-zA-Z]+", a):
            has_c = has_c or (a[0] == "-" and "c" in a)
            i += 1
        elif a.startswith("--"):
            i += 1
        else:
            break
    if has_c and i < len(args) and i not in out:
        out.append(i)
    return out


# T-0892: NAME=$(mktemp -d) is a fresh folder in TMPDIR or /tmp: read as that literal path, so `rm -r $T` later is a
# known target, and `$T/../..` still resolves to what it would remove. T-1888: so is `-d -p DIR` in DIR, and
# `-d DIR/nameXXX` (mktemp prints the path as given); DIR a literal without ~ (HOME inside $( ) even in quotes)
_MKTEMP_D = lazy(r"""(?<![\w$])(\w+=)(["']?)"""
                       r"""(?:\$\(\s*mktemp\s+([^()`'"$\n]*?)\s*\)|`\s*mktemp\s+([^()`'"$\n]*?)\s*`)\2""")
_MKTEMP_ARGS = lazy(r"-d|-d\s+-p\s+(?P<p>[\w./@+:,-]+)|-p\s+(?P<q>[\w./@+:,-]+)\s+-d"
                          r"|-d\s+(?P<t>[\w./@+:,][\w./@+:,-]*?)X{3,}")
# T-0906: a command that names TMPDIR or PATH at all (TMPDIR=, TMPDIR[0]=, ${TMPDIR:=…}), gives a shell an rc file
# (BASH_ENV, ENV, --rcfile, --init-file), defines its own mktemp or repoints it (hash -p, enable, an alias) makes the
# folder somewhere else (after eval or source the guard already treats T as unknown: see the KnownVars cases)
_MKTEMP_MOVED = lazy(r"\b(?:TMPDIR|PATH|BASH_ENV|ENV)\b|--(?:rcfile|init-file)\b|\bmktemp\s*\(\s*\)|"
                           r"\bfunction\s+mktemp\b|\balias\b[^;&|\n]*\bmktemp\b|\b(?:hash|enable)\s")


def _mktemp_literal(cmd):
    if _MKTEMP_MOVED.search(cmd):
        return cmd
    def lit(m):
        a = _MKTEMP_ARGS.fullmatch(m[3] if m[3] is not None else m[4])
        if not a:
            return m[0]
        if a["t"] is not None:
            return m[1] + a["t"] + "mktemp"
        return m[1] + os.path.join(a["p"] or a["q"] or os.environ.get("TMPDIR") or "/tmp", "tmp.mktemp")
    return _MKTEMP_D.sub(lit, cmd)


def _track_vars(words, env, home=None, spaces=False):
    """T-0161: the variables after one simple command (its words as written), or None once any could be unknown.
    spaces (T-4785 round 4, a sampo word's reader): a quoted value holding a space is kept (C="sampo task"; $C splits it
    as bash does) where a path reader drops it."""
    words = [w for i, w in enumerate(words) if not re.match(r"\d*(?:[<>]|&>)", w)  # redirections and their targets
             and not (i and re.fullmatch(r"\d*(?:[<>]+&?|&>>?)\|?", words[i - 1]))]
    runner = False
    while words and words[0] in ("!", "time", "command", "builtin"):  # unquoted: these keep the command in this shell
        runner = runner or words[0] in ("command", "builtin")  # whose `command D=x` runs a program named D=x
        words = words[1:]
        while words and words[0].startswith("-"):
            words = words[1:]
    if words[:1] == ["export"] and all(_ASSIGN.match(w) or re.fullmatch(r"\w+", w) for w in words[1:]):
        words, runner = [w for w in words[1:] if "=" in w], False  # a bare name keeps its value
    if not runner and all(_ASSIGN.match(w) for w in words):
        for w in words:
            k, rv = _ASSIGN.match(w).group(1), w.partition("=")[2]
            # T-0330: an unquoted leading ~ or ~/ in an assignment is the home folder (bash expands it there)
            v = _unquote(home + rv[1:] if home and re.match(r"~(?:/|$)", rv) else rv)
            if k == "IFS":
                return None  # word splitting changes: no expansion can be read
            if _SHELL_SET.match(k) or v is None or _unresolvable(v) or re.search(r"[*?\[~]" if spaces else r"[*?\[~\s]", v):
                env.pop(k, None)  # a glob, a tilde form or a space is expanded or split later: leave it unknown
            else:
                env[k] = v
        return env
    for m in filter(None, map(_ASSIGNISH.match, words)):  # a prefix assignment (it can outlive a special builtin),
        if m.group(1) == "IFS":                            # NAME+= or NAME[i]=, or one a command is handed
            return None
        env.pop(m.group(1), None)
    word = next((w for w in words if not _ASSIGNISH.match(w)), "")
    m = re.fullmatch(r"\$(?:\{(\w+)\}|(\w+))(/[^$`*?\[\s'\"\\{]*)", word)
    if m and (m.group(1) or m.group(2)) in env:  # T-0384: $S/tool with S a known literal (no space, glob or $) is a
        word = env[m.group(1) or m.group(2)] + m.group(3)  # path, and a path is never a builtin
    if re.search(r"[$`*?\[]", word):
        return None  # T-0162: a name bash computes ($X, `…`, a glob) can turn out to be a builtin
    name = _unquote(word) or ""
    rest = words[words.index(word) + 1:] if word in words else []
    return None if _setter(name, rest) and not (name == "trap" and _trap_inert(rest)) else env


_TRAP_SAFE = frozenset(("rm", "rmdir", "unlink", "[", "[[", "test", "exit", "true", "false", "echo", "kill", "wait",
                        "sleep", ":"))


def _trap_inert(args):
    """T-4811: `trap 'BODY' SIG…` sets no name, whenever the signal fires, when BODY runs only commands that can't set
    one (rm, a test, exit, echo …): no computed text, no assignment, no function to call. `w=$(mktemp -d); trap 'rm
    -rf "$w"' EXIT` then keeps w a known folder for the rest of the line, where any trap made every name unknown."""
    if not args or args[0].startswith("-") or len(args) < 2:
        return False
    body = _unquote(args[0])
    if not body or re.search(r"\$\(|`|<\(|>\(|\(\(|\$\{[^}]*=|\$\[", body):
        return False
    cmds = _split(_tokens(_lines(body)))
    return bool(cmds) and all(x.argv and os.path.basename(x.argv[0]) in _TRAP_SAFE for x in cmds)


def _subst_depth(cmd):
    """The deepest run of `$(` open at once, counted on the raw text (quotes ignored: it only has to be an upper bound)."""
    d = m = 0
    for a, b in zip(cmd, cmd[1:]):
        if a == "$" and b == "(":
            d += 1
            m = max(m, d)
        elif a == ")" and d:
            d -= 1
    return m


def _set_once(name, text):
    """T-4811: `name` is written once in text (its one bare mention is the assignment: no `name+=`, `read name`, `for name`,
    `${name:=x}` or a second `name=` follows), so a later reader sees the value that assignment gave."""
    rest = re.sub(rf"\$(?:\{{{name}\}}|{name}(?!\w))", "", text)
    return len(re.findall(rf"(?<![\w$]){name}(?!\w)", rest)) == 1


def _command_words(cmds):
    """T-4811: the commands whose first word is a command name bash computes. `echo $(date) $X` splits as `echo $`, the
    body, then `$X` (op `)`): the words after a substitution's close, which are arguments once an earlier word named the
    command, so only `a=$(x) $X` (and `$(x) $X`) leaves the command word to the remainder. Counted this way, an argument
    holding a substitution no longer makes a line unclean, which kept every tail of it re-read (the long-script cost)."""
    out, named = [], {}  # named[depth]: a command word came before the substitution this remainder continues
    for x in cmds:
        stub = x.argv[-1:] and x.argv[-1].endswith("$")  # the word that opens a substitution is the last of its command
        if x.op != ")" or not named.get(x.depth):
            out.append(x)
        words = x.argv[:-1] if stub else x.argv
        named[x.depth] = (named.get(x.depth, False) if x.op == ")" else False) or any(
            not re.match(r"\w+=", w) for w in words)
    return out


def check_bash(cmd, ctx, depth=0, tails=True, at=False):
    """Return [(category, detail)] for every dangerous thing found in a shell command."""
    if depth > 4:
        return [("rm-outside", "command nesting too deep to analyse: split the command")]
    entry = cmd  # round 9: the text as it came, to tell a read-to-the-end tail of a substitution from a command line
    # T-1727 review: the folders the caller may be in when it runs this text (eval, sh -c, sh <<<), so a discarding
    # git command read here is saved where it runs, not in the hook's cwd
    if not depth:
        _DCWD.clear()  # T-1921 review: fmmcp calls this directly, in a process that lives on
        _FS_MEMO.clear()  # T-4758: what one reading learned from the file system lasts that reading
        _DCWD["line"] = cmd  # T-3600: the whole command, where a loop a nested reading sits in starts (_loop_values)
        if _subst_depth(cmd) > 8:  # T-2668: every level re-reads its tails, so work grows ~n^4 until the cap above
            return [("rm-outside", "command nesting too deep to analyse: split the command")]  # fires: 500 deep hung
    starts = _DCWD.get(depth - 1, [ctx.cwd]) if depth else [ctx.cwd]
    if at is False and starts != [ctx.cwd]:  # T-1921: and judged there, once per folder (None: one it can't name,
        out, seen = [], _DCWD.setdefault("seen", {})  # judged from here with every relative path unknown); a text a
        for s in starts:  # loop repeats is judged once per folder, so only new pairs count toward the cap
            if (cmd, s, depth, tails) not in seen:
                if len(seen) >= 256:
                    return [("rm-outside", "nested commands in too many folders to analyse: split the command")]
                here = object.__new__(Ctx)
                here.__dict__ = {**ctx.__dict__, "cwd": s or ctx.cwd}
                seen[cmd, s, depth, tails] = check_bash(cmd, here, depth, tails, s)
            out += [f for f in seen[cmd, s, depth, tails] if f not in out]
        return out
    starts = starts if at is False else [at]
    _DCWD[depth] = starts
    # the whole command only: a nested shell's text alone would miss what its caller set (BASH_ENV=… bash -c '…')
    cmd = _mktemp_literal(_join_continued(cmd)) if depth == 0 else _join_continued(cmd)
    cmd = _decode_ansi_c(cmd)  # T-1480: $'cat' is cat
    unreadable = _ifs_unreadable(cmd)  # added last, never instead: a granted system mustn't let the rest through
    cmd = _ifs_spaces(cmd)  # T-0576: rm${IFS}-rf ~ is "rm -rf ~" to bash (IFS splits the expansion into words)
    cmd = _expand_literal_loops(cmd)  # T-0411: a loop over literal words, as the commands it runs
    found = (_interpreter_writes(cmd, ctx) + _interpreter_reads(cmd, ctx)  # every depth: an fm --run command is read
             + _interp_links(cmd, ctx))
    # on its own (T-0128 review)
    # T-0345: what python code starts; a script only written with cat isn't run by writing it, unless the same
    # command also runs an interpreter (cat > t.py <<EOF … EOF; python3 t.py)
    dry = _drop_data_heredocs(cmd)
    shell = _strip_heredocs(cmd)
    lits = _line_literals(shell)  # T-0587: literal values a computed name, eval or sh -c text may use
    # T-0158: every `…` and $( … ) a shell runs (unquoted heredoc bodies included), read as a command of its own
    # a tail (the text after a substitution opens) is read once as it stands: its own tails are suffixes of it already
    live = [x for t in _live_heredocs(cmd) for x in ([t, _unescape_ticks(t)] if "\\`" in t else [t])]  # T-0669: in
    cmds, top = _split(_tokens(_lines(shell))), _top(shell)
    found += _installer_lines(cmds, cmd)  # T-3608
    # T-3600 review 4: whether this text's literals hold for sampo words (nested, through the whole line's literals:
    # id=$($R sampo task new …))
    holds = _literals_hold(cmds, shell, {**_DCWD.get("lits", {}), **lits})
    if not depth:  # the whole line's, for a sampo call read nested; none where they may not hold
        _DCWD["holds"] = holds
        _DCWD["lits"] = {}  # set below, once the home is known: only the values whose assignment runs first (_seq_literals)
    # T-1921: an && chain headed by a plain `cd /abs &&` runs nothing where it starts (a failed cd stops it all)
    head = top[1] and re.match(r"\s*cd[ \t]+(/[^\s;&|<>()'\"`$*?\[\]{}\\!#~]*)[ \t]*&&", shell)
    rest, prev = shell[head.end():] if head else "", None
    while rest != prev:  # and nothing after it may move it (substitutions run in subshells: only a word they make may)
        rest, prev = re.sub(r"`[^`]*`|\$\([^()]*\)", "$S", rest), rest
    word = lambda a: next((w for w in a if not re.match(r"\w+=", w)), "")  # its review: the masking above isn't quote
    head = head if head and not _moves_text(re.sub(r"\$\{\w+\}", "$V", rest)) and not any(  # aware, nor sees c$(…) or
        _MOVERS.search(" ".join(x.argv)) or re.search(r"[^\w./+:@%,-]", word(x.argv)) for x in cmds[1:]) else None  # A=1 $C
    _DCWD[depth] = [os.path.normpath(head[1])] if head else list(dict.fromkeys(starts + [  # text read before the walk
        _cd_from(x.argv[1], s, ctx) for x in cmds if x.argv[:1] == ["cd"] and x.argv[1:2] for s in starts] + [
        r for x in cmds if x.argv[:1] == ["cd"] and x.argv[1:2] for s in starts if (r := _cd_real(x.argv[1], s, ctx))]))
    for src, words in _python_sources_argv(cmd if _INTERP.search(dry) else dry):
        last = []
        for spawned in _spawned_commands(src, words):
            if spawned == _ARGV_UNK:  # T-4736: a command from argv the outer words don't spell out (as node's is)
                found += [_SPAWN_UNK] if not last and _SPAWN_UNK not in found else []  # (what the text before it
            else:  # already refuses keeps its own, more exact reason)
                last = _child_check(spawned, ctx, depth + 1)
                found += last  # T-0345, and its review: from those folders too (`…`,
    # sh <<<, python -c): any folder a cd names
    texts, marks = _text_spawned(cmd, ctx, dry)  # T-4686: and the shell strings node, perl, ruby, php, lua, R start
    for spawned in texts:
        found += _child_check(spawned, ctx, depth + 1)
    for mark, finding in (("big", _TOO_BIG), ("unk", _SPAWN_UNK)):
        if mark in marks and finding not in found:
            found.append(finding)
    plain, prev =re.sub(r"(?<![\w./-])cd[ \t]+[\w./+:@%,][\w./+:@%,-]*(?=[ \t]*(?:[;&|)\n]|$))", "", shell), None
    while plain != prev:  # T-1924: the walk names every folder a literal cd may reach; any other mover (cd $(…), $C,
        plain, prev = re.sub(r"`[^`]*`|\$\([^()]*\)", "$S", plain), plain  # eval) keeps each tail, read from the
    clean = not _moves_text(plain) and not any(  # folders before it, as the coarse catch it was (review: c$(…), A=1 $C)
        re.search(r"[^\w./+:@%,-]", word(x.argv)) for x in _command_words(cmds))
    if tails and not clean and re.search(r"\$\(|`", shell):  # T-4811: once more without what only frames a script
        clean = not _moves_text(plain, frames=True) and not any(
            re.search(r"[^\w./+:@%,-]", word(x.argv)) for x in _command_words(_split(_tokens(_lines(_frame_free(shell))))))
    # T-0330 review: ~ is $HOME, so once the command may set HOME a ~ in a value isn't the user's home any more
    tilde = None if re.search(r"(?<![\w${])HOME\b", shell) else ctx.home
    pre = _prefix_vars(shell, cmds, tilde)  # T-0183; also what a path-named command may expand (T-0331)
    # T-4785 round 4: what a sampo word may read of the line's literals, those whose assignment runs before every use
    seq = _seq_literals(shell, cmds, lits, tilde) if holds and not _name_computed(shell) else {}
    if not depth:
        _DCWD["lits"] = seq
    if tails:  # round 9: the texts read to the end of a line, whose stub $ is the tail of "$(a) | $(b)", no command word
        _DCWD.setdefault("tail", set()).update(set(_subst_bodies(shell, True, clean)) - set(_subst_bodies(shell, False, clean)))
    for body in _subst_bodies(shell, tails, clean) + [b for t in live for b in _subst_bodies(t, tails, clean)]:  # `…`
        # T-4138: a body (or the tail after a substitution opens) is read apart from the assignments before it: the
        # names this command sets once in its prefix, which the body uses, go with it (S=/x; cd $(pwd)/y; … > $S/f)
        carried = [f"{k}={v}" for k, v in pre.items() if re.search(rf"\$\{{?{k}\b", body) and re.fullmatch(r"[\w./@:+,=%~-]+", v)]
        if carried and body.count(")") > body.count("("):  # a tail still holds the `)` that closes its substitution:
            body = "(" + body  # reopened, so the text reads as a group, not a stray close the walk can't follow
        found += check_bash("; ".join(carried + [body]) if carried else body, ctx, depth + 1, tails=False)
    for script in _stdin_scripts(cmd, cmds):  # T-0589: bash <<'EOF' … EOF and sh <<< '…' run that text
        found += _child_check(script, ctx, depth + 1)
    cwd, chain = ctx.cwd, []
    raw = _raw_cmds(shell) if _straight_line(shell) else None  # the same commands, quotes kept (T-0161)
    outside, bare = _outside(shell, cmds, ctx, pre)  # T-0175: what it inherits and can't change
    # VAR → literal (T-0151); rm trusts no inherited value but HOME, a write target any (its review)
    env = {k: v for k, v in outside.items() if k == "HOME"} if raw is not None and len(raw) == len(cmds) else None
    (straight, and_chain, mixed), cwds, lost, made = top, [ctx.cwd], [""] if at is None else [], set()  # (T-0175)
    base = None  # T-0339: (cwds, lost, made) where the current &&-segment of a mixed list began, and its folders
    # T-1727 review: the same walk for recording discards only (verdicts use cwds): from the caller's folders, None for
    # one a cd makes unknown; rbase is base's twin
    rd, rbase = list(starts), None
    cdpath = bare is None or "CDPATH" in shell or bool(os.environ.get("CDPATH"))
    fixed = pre if env is None and bare is not None else {}  # T-0183: set once before the
    outside = {**outside, **fixed}                                                  # branches, known in them
    scan = _mask_fm(shell, ctx)  # T-0183: guess from the command's own words, not fm's text arguments
    chmodded = set()  # T-3387: paths an earlier command here gave an exec bit
    wrote, fed = [], []  # T-0275: files earlier commands here write (all; and those a heredoc fills)
    failcd = ""  # T-3713: the last cd that may have failed (its target), named in a brief refusal
    gone = False  # T-1924 review: an earlier command here may remove, rename or relink a folder a later cd names
    fm_elsewhere = fm_lits = None  # T-3600 review: read once a line, when a command word is computed or runs fm
    for idx, c in enumerate(cmds):
        nxt_op = cmds[idx + 1].op if idx + 1 < len(cmds) else ""
        piped = c.op in ("|", "|&") or nxt_op in ("|", "|&")  # T-0355: in a pipeline, a cd moves its subshell only
        in_chain = mixed and not piped and (c.op == "&&" or nxt_op == "&&")
        if mixed and c.op not in ("&&", "|", "|&"):  # a new segment: the last one's chain may have stopped anywhere
            if base:
                cwds, lost = list(dict.fromkeys(base[0] + cwds)), list(dict.fromkeys(base[1] + lost))
                made = base[2]
                rd = list(dict.fromkeys(rbase + rd))
            ops = [x.op for x in cmds[idx + 1:]]
            seg = ops[:next((i for i, o in enumerate(ops) if o not in ("&&", "|", "|&")), len(ops))]
            base = (cwds, lost, set(made)) if "&&" in seg else None
            rbase = list(rd) if base else None
        if env is not None:
            env = _track_vars(raw[idx], env, tilde)
        known = outside if env is None else {**outside, **env}
        argv, via_xargs = _strip_wrappers(c.argv)
        xodd = via_xargs and any(_xargs_opts(c.argv[j + 1:])[1] for j, x in enumerate(c.argv[:len(c.argv) - len(argv)])
                                 if os.path.basename(x) == "xargs")  # T-1123
        if not c.piped:
            chain = []
        name = os.path.basename(argv[0]) if argv else ""
        args = argv[1:]
        cdn, cda, maybe, unsure = (name, args, [], False) if name in _CDS else ("", args, [], False)
        if argv and not cdn and _unresolvable(argv[0]):  # T-1923: a computed command word: a cd if it names one
            w0 = _with_vars(argv[0], known)  # (split as bash splits it), or any command
            w0 = ([w0] if _unresolvable(w0) else w0.split() or [""]) + args
            if w0[0] in _CDS:
                cdn, cda = w0[0], w0[1:]
            elif _unresolvable(w0[0]):  # a cd to its first word, or alone (C="cd /x"; $C, or the $ of an unquoted
                maybe = [_with_vars(a, known) for a in args[:1]] or ["$OLDPWD"]  # $(…) whose words split off) anywhere
                if args[:1] and args[0] in _CDS and not _PART.sub("", w0[0]):
                    cdn, cda, unsure = args[0], args[1:], True  # $X cd D: X empty, eval or builtin moves it to D
        gone = gone or not _keeps_folders(c, argv)
        if name == "mkdir" and "-p" in args:  # a folder this command makes takes a cd as surely as one that exists,
            made.update(d for d in (os.path.normpath(_expand(_with_vars(a, known), ctx)) for a in args)  # when its
                        if os.path.isabs(d) and "$" not in d and _makeable(d))  # nearest existing folder lets it be made
        if cdn:
            dirs = [a for a in cda if a not in ("-P", "-L", "-e", "-@", "--")]
            if cdn == "cd" or "-n" not in dirs:  # pushd/popd -n change only the stack
                unknown = cdn == "popd" or dirs[:1] == ["-"] or (
                    cdn == "pushd" and (not dirs or re.fullmatch(r"[+-]\d+", dirs[0])))  # rotations, a swap, cd -
                tgt = "$OLDPWD" if unknown else _with_vars(dirs[0], known) if dirs else ctx.home
                if not _unresolvable(tgt):
                    cwd = _resolve(_expand(tgt, ctx), cwd)
                d = "" if _unresolvable(tgt) else _expand(tgt, ctx)
                lands = [os.path.normpath(d)] if os.path.isabs(d) else [] if not d or lost or cdpath else [
                    _resolve(d, b) for b in cwds]  # T-1924: a relative one too, from folders it can name (each
                child = unsure or any(a not in _KEYWORDS and a not in ("time", "command", "builtin", "noglob") and not re.match(
                    r"[A-Za-z_]\w*\+?=", a) for a in c.argv[:len(c.argv) - len(argv)])  # review: nice cd X moves a child
                can_fail = gone or child or len(dirs) > 1 or not lands or not all(x in made or (  # review: cd a b fails
                    os.path.isdir(x) and os.access(x, os.X_OK)) for x in lands)  # (too many arguments); `cd ..` doubled
                certain = not child and not piped and c.op != "||" and (  # T-1891: a cd after || may be skipped
                    and_chain or in_chain or ((straight or mixed) and not can_fail))
                failcd = shlex.quote(tgt) if can_fail and not certain and not _unresolvable(tgt) else failcd
                before = cwds
                cwds, lost = _moved(cwds, lost, tgt, certain, cdpath, ctx)
                new = [_cd_from(tgt, r, ctx) for r in rd] + [x for r in rd if (x := _cd_real(tgt, r, ctx))]
                if dirs and not unknown and _unresolvable(tgt) and _only_unset(tgt, bare):  # T-1923: bash makes it
                    rest = _PART.sub("", tgt)  # empty: cd "$X" stays put, cd $X goes home (or to the word after it)
                    ws = [w for w in raw[idx] if _unquote(w) == dirs[0]] if raw is not None and len(raw) == len(cmds) else []
                    to = [rest] if rest else ([] if ws and all(w == dirs[0] for w in ws) else ["."]) + (
                        [] if ws and all(w != dirs[0] for w in ws) else [dirs[1] if len(dirs) > 1 else ctx.home])
                    cwds = list(dict.fromkeys(cwds + [p for x in to for p in _moved(before, [], x, True, False, ctx)[0]]))
                    new += [_cd_from(x, r, ctx) for x in to for r in rd]
                if cdpath and d and not os.path.isabs(d) and not re.match(r"\.\.?(/|$)", d):  # T-1923: CDPATH may send
                    new += [None] + [p for w in _line_words(scan, known) for s in w.split(":") if s for p in [_resolve(  # it
                        d, _resolve(_expand(s, ctx), ctx.cwd))] if os.path.isdir(p)]  # below a folder the line names
                    # (each entry of a CDPATH=a:b list): nested text runs there
                rd = list(dict.fromkeys(new if certain else rd + new))
                if base and c.op not in ("&&", "|", "|&") and not piped and not can_fail:
                    base = (list(cwds), list(lost), base[2])  # T-0384: a head cd that can't fail: the chain stops after
                    rbase = list(rd)
                elif base:  # the chain may stop after any cd in it: every folder it reaches stays possible (its review)
                    base = (base[0] + cwds, base[1] + lost, base[2])
                    rbase = rbase + rd
        moves, places = (_moves_shell(name, args, cwds, ctx, shell) if name in ("eval", "source", ".", "trap",
                                                                               "mapfile", "readarray") else
                         (1, maybe) if maybe else (0, []))
        if moves:  # T-0384: it may cd here: anywhere, or any folder it names after a cd
            for d in ["$OLDPWD"] + places:
                cwds, lost = _moved(cwds, lost, d, False, cdpath, ctx)
            rd = list(dict.fromkeys(rd + [_cd_from(d, r, ctx) for d in places for r in rd]))  # cd - aside
            if base:
                base = (base[0] + cwds, base[1] + lost, base[2])
                rbase = rbase + rd
        here_dirs = _DCWD[depth] = rd  # T-1727 review: where this command may run (and nested text start)
        if len(cwds) + len(lost) + len(rd) > 1024:  # T-1924: cds that may each fail double these (a line that
            # isn't straight: if, ( ), | and ||): refuse in milliseconds, not minutes
            return found + [("rm-outside", "the line may be in too many folders to analyse: split the command")]
        # T-4785 round 6: the -c text is a child process: it sees this command's env prefix (V=x sh -c …), not the line's
        handed, parent_holds = _handed(c.argv, argv, lits), _DCWD.get("holds") and holds
        for inner in (x for t in _shell_c(args) for x in _readings(t, {**lits, **known})) if _C_SHELLS.match(name) else ():
            found += _child_check(inner, ctx, depth + 1, handed, parent_holds)
            if _DOWNLOAD_SUBST.search(inner):
                found.append(("pipe-shell", f"{name} -c runs a downloaded script"))
        # T-0160 (differential test against bash): commands these run as text or as their own arguments
        if name == "trap" and args and not args[0].startswith("-"):
            # T-4811: the body runs later in this shell, so the names this line sets once (a mktemp folder, a literal)
            # keep their value in it, as in a $( ) body (T-4138): `w=$(mktemp -d); trap 'rm -rf "$w"' EXIT` is a temp rm
            used = {k: v for k, v in {**known, **pre}.items() if re.fullmatch(r"[A-Za-z_]\w*", k)
                    and re.search(rf"\$\{{?{k}\b", args[0]) and re.fullmatch(r"[\w./@:+,=%~-]+", v)}
            carried = [f"{k}={v}" for k, v in used.items() if k in pre or _set_once(k, shell)]
            found += check_bash("; ".join(carried + [args[0]]) if carried else args[0], ctx, depth + 1)
        if name == "script":
            for j, a in enumerate(args[:-1]):
                if a in ("-c", "--command") or re.fullmatch(r"-[a-zA-Z]*c", a):
                    found += _child_check(args[j + 1], ctx, depth + 1, handed, parent_holds)
        if name == "find":
            for j, a in enumerate(args):
                if a in ("-exec", "-execdir", "-ok", "-okdir"):
                    end = next((k for k in range(j + 1, len(args)) if args[k] in (";", "+")), len(args))
                    found += check_bash(" ".join(shlex.quote(x) for x in args[j + 1:end]), ctx, depth + 1)
        if name == "eval":
            joined = " ".join(args)
            for text in _readings(joined, {**lits, **known}):  # T-0587: q='rm -rf'; eval "$q ~"
                found += check_bash(text, ctx, depth + 1)
            for m in _PRINTED_SUBST.finditer(joined):  # T-1123: eval "$(printf 'rm -rf ~')" runs the printed text
                try:
                    w = shlex.split(m.group(2))
                except ValueError:
                    w = []
                cmdname, w = (w[0], w[1:]) if w else ("", [])
                if cmdname == "echo":
                    w = [a for a in w if a != "-n"]
                if w and (cmdname == "echo" and not any(a.startswith("-") for a in w[:1] if a != "--") or
                          cmdname == "printf" and not re.search(r"[%\\]", w[0])):
                    found += check_bash(" ".join(w), ctx, depth + 1)
                else:  # flags, escapes, a format, a wrapped or pathed echo: the text isn't what is written
                    found.append(("rm-outside", "eval of computed text it can't analyse: run the text itself"))
            if _DOWNLOAD_SUBST.search(joined):
                found.append(("pipe-shell", "eval of a downloaded script"))
        computed = bool(re.search(r"[$`]", name))  # `| $GREP pat` is common: any word after it counts as its script
        # ponytail: a bare `$` name inside a substitution reading is the read-to-the-end tail of "$(a) | $(b)" in
        # quotes, not a pipe; a real `| $(echo bash)` nested inside another $( ) slips by (top level is refused)
        # T-0982: inside a substitution reading (depth > 0: a "$( )" tail read to the end, odd quotes and all), a
        # "name" holding a quote or a redirect is debris of the quote-blind fallback split, e.g. awk's program
        # $1<1200'. The top-level command is still read whole, so a real | python3 there is still refused
        debris = bool(depth and re.search(r"['\"<>]", name))
        # T-3693: a pathed command fed by a pipe is read as the file it runs too (./cat linked to bash is a shell)
        sh = next((b for b in map(os.path.basename, _real_files(argv[0], cwds) or []) if _SHELLS.match(b)),
                  None) if argv and (c.piped or "|" in c.op) else None
        if c.piped and not debris and not (name == "$" and depth) and _reads_stdin_code("bash" if computed else sh or name, args, via_xargs, any_word=computed) and not (
                # T-0715: echo 'rm -rf ~' | bash; T-0716: | (sh), | $SH (a computed name may be a shell); a
                any(_name(x.argv) in _FETCHERS for x in chain) or _FETCH_WORD.search(_mask_quotes(cmd))):  # download
            # piped in is pipe-shell's (below), which the user can grant for an installer
            found.append(("system", f"{name} reads its program from a pipe, which the guard can't read: put it in a "
                                    f"heredoc ({name} <<'EOF' … EOF), which it reads, or in a file"))
        if _SHELLS.match(sh or name) or name in ("source", "."):
            # T-0421: a download anywhere on the line counts (a $( ), a group or a <( ) splits it from the shell's own
            # chain; a file downloaded first and then piped in is a download too), and so does a pipe into a group
            fetched = any(_name(x.argv) in _FETCHERS for x in chain) or _FETCH_WORD.search(_mask_quotes(cmd))
            if (c.piped or "|" in c.op) and fetched and not _reads_pipe_as_data(c, cmds, cwds, cmd):
                why = f"downloaded content piped into {name}"
                if _reads_pipe_as_data(c, cmds, cwds, cmd, ignore_writes=True):  # T-3673: only the others fail it
                    why += (": run the `curl … | python3 -c …` pipeline as its own command (a for or while loop of it "
                            "and sleep passes too), since other commands on this line write files and could plant a "
                            "module python imports")
                found.append(("pipe-shell", why))
            nxt = cmds[idx + 1] if idx + 1 < len(cmds) else None
            if c.procsub and nxt and _name(nxt.argv) in _FETCHERS:
                found.append(("pipe-shell", f"{name} <(download)"))
            elif c.procsub and nxt and not _positionals(args):  # T-3600 review: source <(echo sampo serve)
                found.append(("system", f"{name} <(…) runs what the substitution prints, which the guard can't read: "
                                        "run that command itself"))
        git_env = [a.split("=", 1)[1] for a in c.argv if name == "git" and a.startswith(("GIT_DIR=", "GIT_WORK_TREE="))]
        if name == "git" and any(re.match(r"(?i)GIT_CONFIG_(KEY_\d+|PARAMETERS)=.*core\.hookspath", a) for a in c.argv):
            found.append(("system", "git with core.hooksPath set through the environment"))
        # T-0269/T-0270: a command git (or a program) will run on its own is checked as if typed; config it would load
        # from elsewhere is refused; a file a variable makes it write is a write target
        for why, code in _env_channels(c.argv, name) + (_git_code(args) if name == "git" else []) + _exec_options(
                name, args):
            found += [("system", why)] if code is None else _child_check(code, ctx, depth + 1)
        # T-0275: the file a shell, source, python or an executable with a #! line is told to run
        kind, script = _script_target(name, argv[0] if argv else "", args)
        if script:
            norm = lambda w: os.path.normpath(os.path.join(cwd, os.path.expanduser(w)))  # noqa: E731
            path = norm(script)
            if path in {norm(w) for w in fed} and kind != "py":  # cat > f <<'EOF' … EOF; bash f: the body is the script
                for body in _heredoc_split(cmd)[1]:  # (which heredoc wrote it isn't modelled: all are read, as for bash <<EOF)
                    found += check_bash("\n".join(body), ctx, depth + 1)
            # a python script written by a heredoc in this command is read by the heredoc scan (T-0345); a path-named
            # command is refused only when it looks like a script: curl -o x; ./x is a download, not a script
            elif path in {norm(w) for w in wrote} and not (kind == "py" and "<<" in cmd) and (
                    kind != "exec" or script.endswith((".sh", ".bash", ".zsh", ".py", ".ksh")) or _chmodded(path, chmodded)):
                found.append(("system", f"{script} is written earlier in this command and run here, which the guard "
                                        "can't read: write it in one command, run it in the next"))
            else:
                found += _script_verdict(script, cwd, kind, ctx, depth, assume_x=_chmodded(path, chmodded))
        if name == "chmod":  # chmod +x F && F: F runs as an executable though it isn't one yet
            mode = next((a for a in args if not a.startswith("-") or re.match(r"-[rwxXstugo+=,-]*[+=]", a)), "")
            if re.search(r"[+=][rwXstugo]*x|^[0-7]*[1357][0-7]{0,2}$", mode):
                rec = any(a == "--recursive" or re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*", a) for a in args)
                for a in args:
                    if a == mode or a.startswith("-"):
                        continue
                    if re.search(r"[*?\[$`]", a):
                        chmodded.add("*")  # a glob or variable: any path in this command may be the one
                    else:
                        t = os.path.normpath(os.path.join(cwd, os.path.expanduser(a)))
                        chmodded.add(t + "/" if rec else t)
                        chmodded.add(t)
        fed +=c.redirs if any(op == "<<" for op, _ in c.feeds) else []
        # an in-place edit (sed -i FILE && python3 FILE) is the everyday edit-then-run: only a file made here counts
        wrote += c.redirs + ([] if name in ("sed", "gsed", "perl", "ed", "ex", "touch")  # touch: an empty file
                             else _write_targets(name, args))
        unlinks = set(_write_targets(name, args)) if name == "rm" else set()  # T-0412: rm of a link unlinks it
        # T-0859: a bare git init where the cwd the guard resolves already holds .git rewrites nothing
        init = name == "git" and args[:1] == ["init"]  # no global options: -C and --git-dir move the target
        # security review: git takes unique prefixes of long options (--templ= is --template) and GIT_* env can plant
        # a template dir, so only `git init` / `git init -q` qualify, and only when nothing in the command sets GIT_*
        bare_init = init and not lost and not git_env and all(a in ("-q", "--quiet") for a in args[1:]) and all(
            os.path.exists(os.path.join(d, ".git")) for d in cwds or [ctx.cwd]) and not re.search(
            r"(?:^|[\s;&|(`])(?:export\s+|env\s+)?GIT_\w*=", cmd)
        # T-1195: an environment that changes how git reads pathspecs makes a named-file checkout a tree write
        # (anywhere in the command text: export, env, an earlier assignment)
        targs = args + ["--pathspec-from-file"] if name == "git" and re.search(
            r"(?:^|[\s;&|(`])(?:export\s+|env\s+)?GIT_(?:GLOB|ICASE)_PATHSPECS=", cmd) else args
        # T-1680: touch makes what doesn't exist yet; on a file that does, it changes only the times (a redirect empties it)
        # (review) unless the command may remove or move a file first (unlink P && touch P): existence is read now
        touched = set(_write_targets(name, args)) - set(c.redirs) if name == "touch" and not any(
            _name(x.argv) in _REMOVERS for x in cmds) else set()
        # T-1487: git init's folder named by a variable the guard can't resolve says so, not that init writes its folder
        unset = next((m[0] for t in _write_targets(name, args) if (m := _PART.search(_with_vars(t, known)))),
                     None) if init else None
        hint = (f"; {unset} unresolved: set it to a literal path earlier in the command, or pass the path" if unset
                else '; git init writes its folder: chain the cd with && or pass the path, git init "$S/toy"') \
            if init else ""
        # T-3537: a build-branch merge in a lane's own root is no edit of the project it sits in (brief), as one at the
        # project's root isn't; confine and every other target are judged as before
        # (security review: and only where nothing on the line may set a GIT_* name, exported or inline, which can move
        # the work tree or git dir: bare is None when a builtin may set any name; at depth 0, so no outer text set one;
        # git typed bare, as the command word: a wrapper or assignment prefix (env ${a}_DIR=…, env -C DIR) may move it)
        lane = c.argv[:1] == ["git"] and argv == c.argv and not depth and not git_env and not lost and \
            bare is not None and not any(n.startswith("GIT_") for n in bare) and (
                _lane_merge(args, cwds) or _briefed_lane_merge(args, cwds, ctx) and all(
                    x.op in ("", "&&") for x in cmds[:idx + 1]))  # T-4340: no `cd L || true; git merge`, no `;` or | before it
        for target in c.redirs + ([] if bare_init else _write_targets(name, targs)) + git_env + _env_files(c.argv):
            how = classify_unlink if target in unlinks and not target.endswith("/") else _classify_made if \
                target in touched else classify_write
            found += [(f[0], f[1] + (f" (the cd to {failcd} may have failed)" if failcd and f[0] == "brief" and
                                     isinstance(f[1], str) and not f[1].endswith(")") else "")) for f in _target_cats(
                target, known, bare, cwds, lost, scan, ctx, how, f" ({hint[2:]})" if init and target not in c.redirs else "")
                      if not (lane and target == "." and f[0] == "brief")]
            found += _cargo_config_hit(target, cmd, cwds or [ctx.cwd], ctx)  # T-3607 review
        for target in _link_sources(name, args):  # T-4666: a hardlink's source is one inode with the new name
            found += _target_cats(target, known, bare, cwds, lost, scan, ctx, classify_hardlink,
                                  " (hardlinked: the new name is the same file)", guess=False)
        merge = name == "git" and _git_sub(args) == "merge"  # T-0394: its refusal names sampo lane merge; T-3553: one the
        away = next((d for d in cwds if d != ctx.cwd and os.path.isfile(os.path.join(d, ".git"))), None) if merge \
            and len(cwds) > 1 else None  # walk also reads where the line began, though a cd named a worktree: that cd
        for target in [] if bare_init else _tree_targets(name, targs) + git_env:
            found += _target_cats(target, known, bare, cwds, lost, scan, ctx, classify_tree, " (a tree write over it" + (
                "; git merge" + (f", the cd to {away} may not hold" if away else "") if merge else "") + ")" + hint)
        for target in _build_outs(name, args, c.argv):  # T-3607: where cargo or rustc writes
            found += _target_cats(target, known, bare, cwds, lost, scan, ctx, _build_cats,
                                  " (written by a cargo, rustc or rustup call)")
        wrap = c.argv[:len(c.argv) - len(argv)]
        if name == "cargo" and (sets := _cargo_settings(args, cmds, wrap)) and _builds_native(
                args, wrap, cwds, lost, known, ctx):  # T-3607
            found.append(("core", f"a cargo build of plugin/native with {', '.join(sets)} set (what it compiles, or "
                                  "with what: its target/ is what the installer ships)"))
        # T-1224: the files it reads (operands, a < redirect), what it sends out, a recursive or world-writable mode;
        # an unknown name is guessed from the words the command names, as a write's is (security review: for f in
        # ~/.ssh/*; do cat $f; done read every key)
        for target in _read_targets(name, args) + ([c.stdin] if c.stdin else []):
            found += _target_cats(target, known, bare, cwds, lost, scan, ctx, classify_read, " (read)")
        if name in _SENDERS:
            files, stdin = _sent_files(name, args)
            for target in files:
                found += _target_cats(target, known, bare, cwds, lost, scan, ctx, classify_read, f" (sent by {name})")
            if stdin and c.piped and any(_prints_environment(x) for x in chain):
                found.append(("credentials", f"the environment piped into {name}, which sends it off the machine"))
        targets, recursive = _mode_targets(name, args)
        for target in targets:
            found += _target_cats(target, known, bare, cwds, lost, scan, ctx,
                                  lambda p, x: classify_system_mode(p, x, recursive),
                                  f" ({name} {'recursive' if recursive else 'opening write to others'} on a system "
                                  "folder)", guess=False)
        # T-3600: a computed command word that is sampo by a substitution naming it ("$(command -v sampo)" serve, `which
        # sampo` serve, S=$(command -v sampo); $S serve; a bare $ is an unquoted $( ) the split left behind), its verb
        # maybe in the word itself; one the line sets to a literal is read as its value below (T-0587). The words are
        # read through the values known here and those the line sets once to a literal (T-0587's lits: C="a b")
        fm_call = _fm_argv(argv)
        # review: a nameref (declare -n r=V) makes $r V's value, while the line literal r=V reads as the name V
        if (computed or fm_call) and fm_lits is None:
            # round 7: in nested text (eval's body, a child's) an inherited name the text sets again in a way `seq` can't
            # credit (V=$(…), a branch, read V) is no longer the parent's value
            fm_lits = {**(_inherited(_DCWD.get("lits", {}), shell) if depth else _DCWD.get("lits", {})), **seq} \
                if _DCWD.get("holds") and holds else {}
        fm_vals = _fm_values({**fm_lits, **known}) if computed or fm_call else {}
        if computed and fm_elsewhere is None:  # review: sampo named other than as a command run here (read C <<<
            fm_elsewhere = _fm_named_elsewhere(shell, cmds)  # 'sampo serve', f sampo serve): once a line
        fm_computed = computed and not debris and not (name == "$" and depth) and _unresolvable(
            _with_vars(argv[0], fm_vals)) and (fm_elsewhere or _fm_by_subst(
                shell if name == "$" else " ".join(argv), argv[0], shell))
        if (name == "alias" and any("=" in a for a in args) or any(w.startswith("BASH_ALIASES") for w in c.argv)) and (
                _FM_WORD.search(shell) or any(_unliteral(a.partition("=")[2]) for a in args)):
            found.append(("system", "an alias on a line that names sampo (or one the shell fills in): the guard can't "
                                    "read which sampo verb its name runs, so type the sampo command itself"))
        if fm_computed or fm_call:
            fm_args = _fm_known(argv if fm_computed else args[1:] if name not in FM_PROGRAMS else args, fm_vals,
                                xodd if via_xargs else None)
            unread, kind = _fm_unread(fm_args)
            # round 8: a loop's words are read for a text sampo runs or keeps (kind "run"), never for the verb chain
            var, vals = _loop_values(unread, _DCWD.get("holds") and holds) if unread and kind == "run" else (None, None)
            for v in vals or ():  # T-3600: a loop over literal words (--verify "cmd $t"): read once per word
                found += check_bash(" ".join(([] if fm_computed else ["sampo"]) + [
                    shlex.quote(a) for a in _fm_known(fm_args, {var: v})]), ctx, depth + 1)
            if unread and not vals and kind == "verb":
                found.append(("system", f"{unread} in a sampo command is filled in by the shell at run time, so the "
                                        "guard can't read which sampo verb or gated word it is: type the verb literally"))
            elif unread and not vals:  # review 2: a text sampo runs or keeps as a command, holding an expansion
                x = (_DOLLAR.search(unread) or re.search(r"`[^`]*`?|\S+", unread))[0]
                found.append(("system", f"this text becomes a command sampo runs and holds {x}, which the guard can't "
                                        "read: write the value into the command, or pass the id as its own word (task "
                                        'ac "$T" add "x" --verify "cmd")'))
            if any(_is_allow(a) and (a.partition("=")[2] or b) in USER_ONLY for a, b in zip(fm_args, fm_args[1:] + [""])):
                found.append(("self-authorize", "an agent may not grant core or remote"))
            sub, rest = _fm_subcommand(fm_args)
            # commands fm runs on Claude's behalf (evidence --run, stored gates) get the checks a typed one would
            for a, b in zip(fm_args, fm_args[1:] + [""]):
                flag, eq, val = a.partition("=")
                if flag.startswith("--r") and "--run".startswith(flag):
                    found += check_bash(val if eq else b, ctx, depth + 1)
                elif flag == "--run-file":  # T-4139: the script's own text, judged with the script rules as bash FILE is (not the typed-run rules)
                    found += _run_file_text(val if eq else b, ctx, depth)
            for stored in _fm_runs(sub, rest, fm_args):  # T-0901, T-0898: a command fm keeps or runs
                found += check_bash(stored, ctx, depth + 1)
            if sub == "plugins" and rest[:1] and rest[0] in ("install", "enable", "disable", "add-marketplace"):
                target = plugin_mark(_one_plugin([a for a in rest[1:] if not a.startswith('-')])) \
                    if rest[0] in ("install", "enable") else ""
                found.append(("plugin", f"sampo plugins {rest[0]} changes Claude Code's plugins{target}"))
            if sub == "night" and any(_user_only_hit("--install", a) for a in rest):  # T-2357: plugin is the user's yes
                found.append(("plugin", "sampo night --install puts timers on this machine that run Sampo unattended "
                                        "(sampo night --stage queues the request for the user)"))
            if sub in USER_ONLY_FM and any(_user_only_hit(USER_ONLY_FM[sub][0], a) for a in rest) \
                    and not (sub == "pause" and _pause_is_agents(ctx)):  # flag order is free
                word, why, typed = USER_ONLY_FM[sub]
                found.append(("self-authorize", f"{why} (they can type ! {typed})"))
            # T-4785 round 3: a builder's words as typed, not through the values the line sets (a V=x after the call, in a
            # subshell or behind a && that doesn't run leaves $V unset in the shell: it is read here as x); a builder
            # types the literal. Round 4: and the verb `check` itself too: V="check add"; sampo $V list is `check` through
            # the value but its typed rest is `list`, so a typed verb that isn't `check` while the value makes it one is
            # refused whole (the same value-free reading decides whether to look and what to look at)
            typed = _fm_known(argv if fm_computed else args[1:] if name not in FM_PROGRAMS else args, {},
                              xodd if via_xargs else None)
            tsub, trest = _fm_subcommand(typed)
            if typed != fm_args:  # round 7: a command fm keeps or runs is read as typed too (its verb chain is no value's)
                again = [val if eq else b for a, b in zip(typed, typed[1:] + [""])
                         for flag, eq, val in [a.partition("=")] if flag.startswith("--r") and "--run".startswith(flag)]
                for stored in dict.fromkeys(again + list(_fm_runs(tsub, trest, typed))):
                    found += check_bash(stored, ctx, depth + 1)
                for path in [val if eq else b for a, b in zip(typed, typed[1:] + [""])
                             for flag, eq, val in [a.partition("=")] if flag == "--run-file"]:
                    found += _run_file_text(path, ctx, depth)  # T-4139: the typed words name the same script
            if getattr(ctx, "confine", None) and (sub == "check" or tsub == "check") and (why := (
                    _builder_check(trest + (["<("] if c.procsub else [])) if tsub == "check" else  # a <( ) splits it:
                    "sampo check named by a word the shell fills in at run time (the guard can't read which gate "  # a word comes after
                    "verb it is)")):
                found.append(("confine", why))  # T-4785: a builder never changes the project's gates
                confined = True  # its own reason says more than the general one below
            else:
                confined = False
            # T-4785 round 7: and the verb chain is read as typed for every session (no credited value), so the shell
            # can't run a verb the guard read as another: `V=status; sampo $V risk off` is refused here
            if not confined and not (unread and kind == "verb") and (chained := _fm_chain_unread(typed)):
                found.append(("system", f"{chained} in a sampo command is named by a word the shell fills in at run "
                                        "time (a variable, a substitution, a glob), whatever value the line gave it: the "
                                        "guard reads the verb, its sub-verb and the words its rules key on as typed, so "
                                        "type them literally"))
            if sub == "agents" and "uninstall" in rest[:2]:  # T-0340: it takes the guard out of another agent
                found.append(("core", "sampo agents uninstall removes Sampo's guard from another coding agent"))
            if sub == "serve" and _fm_subcommand(rest, takes_value=("--permission-mode",))[0] not in ("status", "stop"):
                found.append(("remote", "sampo serve starts a persistent Remote Control session reachable from the "
                                        "user's claude.ai account"))
            # T-3592: the action is a choice, matched whole, so any word cap is it wherever options put it (-p x cap);
            # a word the shell fills in at run time could be cap
            words = [_with_vars(a, known) for a in rest] if sub == "cpu" else []
            if "cap" in words or any(_unresolvable(w) for w in words):
                found.append(("system", "sampo cpu cap sets a CPU quota on this session's systemd scope (systemctl "
                                        "--user set-property)"))
        if name in _AWKS:
            found += _check_awk(args, ctx, depth, cwd, cmd)
        if name in ("touch", "mkdir", "mv") or (name == "install" and any(a in ("-d", "--directory") for a in args)):
            # T-1585 review: what these make or take away in a system folder (mv's sources too); elsewhere they stay
            # ordinary, so a mkdir -p build needs no task
            valued = {"touch": ("-r", "--reference", "-d", "--date", "-t"), "mkdir": ("-m", "--mode"),
                      "install": ("-m", "--mode", "-o", "--owner", "-g", "--group"), "mv": ("-S", "--suffix")}[name]
            ops = [a for i, a in enumerate(args) if not a.startswith("-") and not (i and args[i - 1] in valued)]
            for t in ops:
                p = _resolve(_expand(_with_vars(t, known), ctx), cwd)
                if _unresolvable(t):  # ponytail: D=$(echo /etc); touch $D/x isn't read
                    continue
                if re.search(r"[*?\[]", p):  # touch /et[c]/x: the folders the pattern names
                    import glob
                    cands = glob.glob(os.path.dirname(p)) or [p]
                else:
                    cands = [p]
                if any(_system_path(v, ctx) for q in cands for v in _variants(q)):  # a link into /etc counts
                    found.append(("system", f"{p} ({name} in a system folder)"))
        if _unconfines(c.argv):
            found.append(("self-authorize", "it sets FOREMAN_CONFINE, the user's switch for running sampo code workers "
                                            "without Landlock confinement; only the user sets it, in their own shell"))
        if _hides_claude(c.argv):
            found.append(("self-authorize", "it unsets or overrides CLAUDECODE or CLAUDE_CODE_ENTRYPOINT, which fm "
                                            "reads to know Claude, not the user, runs it"))
        if _types_switch(c.argv, shell):
            found.append(("self-authorize", "it types /fm-risk or /fm-trust into a terminal (or starts claude with one as "
                                            "its prompt): the prompt hook answers those as the user's own words, so "
                                            "only the user types them"))
        rm_vars = env or fixed  # T-0190: a literal set once before a branch holds for rm as for a write
        names, val = [name], None
        if re.search(r"[$`]", name):  # T-0587: a name bash computes, read every way it may run
            m = re.fullmatch(r"\$(?:\{(\w+)\}|(\w+))", argv[0])
            val = ({**lits, **known}.get(m.group(1) or m.group(2)) if m else None)
            if val is None and not m:  # round 9 (a): a word of several parts ($x$y, "$x"po) every one a credited literal
                joined = _with_vars(argv[0], {**fm_lits, **known})  # the order-checked ones: a use before its assignment is no value
                val = None if _DOLLAR_OR_TICK.search(joined) else joined
            rest_args = [shlex.quote(a) for a in args]
            typed_args = _typed_args(args)  # round 9: its words as typed, an expansion among them kept for the verb chain
            if val is not None:  # its value on this line, split as bash splits it unquoted
                found += check_bash(" ".join([val] + rest_args), ctx, depth + 1)
                if any(os.path.basename(w) in FM_PROGRAMS for w in val.split()):  # sampo's own verb chain is read as typed
                    found += check_bash(" ".join([val] + typed_args), ctx, depth + 1)
            elif argv[0].count("`") % 2 and not argv[0].startswith("`"):
                # the end of a `…` that opened in an earlier word (L=`touch f`, `a; true`): the split cut the span, so this
                # word is inside it, not a command; what follows it is read as a line of its own
                found += check_bash(" ".join(args), ctx, depth + 1) if args else []
            elif (not debris and not _is_debris_word(argv[0], shell) and not _is_argument_stub(cmds, idx)
                  and not (name == "$" and entry in _DCWD.get("tail", ()))):
                # round 9 (b): a value the guard can't read may be sampo or fm (their tables are the same): the rest of the
                # line is read as if it were, and refused when that reading is. Where that rest can't be found it is $__u
                for rest in _computed_rests(cmds, idx, argv):
                    for _, why in _read_as_sampo(rest, ctx, depth):
                        hit = ("system", f"{argv[0]} is a command word the shell fills in at run time, so it may be "
                                         f"sampo: read as sampo, {why}")
                        found += [hit] * (hit not in found)
            found += check_bash(" ".join(rest_args), ctx, depth + 1) if args else []  # it may expand to nothing
            for t in _shell_c(args) + ([" ".join(args)] if args else []):  # T-0668 (review): or be bash -c, or eval
                for x in _readings(t, {**lits, **known}):
                    found += check_bash(x, ctx, depth + 1)
            if val is None or re.search(r"(?<![\w$])IFS=", cmd):  # T-3795: a value set once on the line is that value
                names += _COMPUTED_AS  # or be any dangerous command
        if not debris and re.search(r"[$`]", "".join(c.argv)):
            # round 11, a net under every position: an expansion anywhere in a command, followed by literal words that read as
            # a refused sampo verb chain (risk off, pause) is refused, whichever word the tokenizer made the expansion. Only
            # a literal chain: an unknown one is the (b) reading's. A position missed there matters only to a doubly
            # computed line
            for w, after in _stub_rests(cmds, idx):
                if _is_debris_word(w, shell) or w == "$" and entry in _DCWD.get("tail", ()):
                    continue
                for _, why in _read_as_sampo(after, ctx, depth):
                    hit = ("system", f"{w} is filled in at run time and literal words follow it that read as a sampo "
                                     f"command: read as sampo, {why}")
                    found += [hit] * (hit not in found)
        for nm in names:
            if nm == "rm" != name and not _rm_opts_only(args):  # GNU rm stops at an option it doesn't know
                continue                                      # (-fwrapv, -Wno-unused…): nothing is deleted
            for w in dict.fromkeys([cwd] + cwds):  # T-1923: in every folder the shell may be (cd $X: home, too)
                found += [f for f in _check_rm(nm, [_with_vars(a, rm_vars) for a in args] if rm_vars else args,
                                               via_xargs, chain, w, ctx, xodd) if f not in found]
            found += _check_git(nm, args, cwd, ctx)
            if nm == "git":  # T-1482: what the hook saves, in each folder it may run in, through known names
                wargs = [_with_vars(a, known) for a in args]
                _DISCARDS.extend((d and w, x) for d in here_dirs for w, x in _git_discards(wargs, d or "/"))  # d None:
                # the folder is unknown, and the hook says so rather than saving its own
            sysf = _check_system(nm, args)
            if sysf and _shlex_fails(cmd):  # T-3684: read off the quote-blind split; the refusal says so
                _FELL_BACK[:] = [1]
            found += sysf
            found += _check_claude_config(nm, args, cmd if c.piped or "<<" in cmd else "")
            found += _check_publish(nm, args)
        # T-1480: a pipe's later commands see the name's value on this line (C=env; $C | curl pipes env); the checks
        # above still read it as computed too, as the value may not be the one it has when it runs
        chain.append(Cmd(c.argv if val is None else c.argv[:len(c.argv) - len(argv)] + val.split() + args, c.redirs,
                         c.piped))
    return found + [("system", unreadable)] if unreadable else found


def _opt_values(args, *flags):
    """Values of options given as `-X v`, `-Xv`, `--long v` or `--long=v`."""
    out = []
    for i, a in enumerate(args):
        for f in flags:
            if a == f and i + 1 < len(args):
                out.append(args[i + 1])
            elif f.startswith("--") and a.startswith(f + "="):
                out.append(a[len(f) + 1:])
            elif not f.startswith("--") and a.startswith(f) and len(a) > len(f):
                out.append(a[len(f):])
    return out


def _tar_targets(args):
    """Where tar writes: the -C dir (else the cwd) when extracting, the archive when creating."""
    short = [a[1:] for a in args if a.startswith("-") and not a.startswith("--")]
    if args and not args[0].startswith("-"):
        short.append(args[0])  # old style: tar xzf archive
    longs = {a.split("=")[0] for a in args if a.startswith("--")}
    out = []
    if any("x" in c for c in short) or longs & {"--extract", "--get"}:
        out += _opt_values(args, "-C", "--directory") or ["."]
    if any(set(c) & set("cruA") for c in short) or longs & {"--create", "--append", "--update"}:
        out += _opt_values(args, "--file") + [args[i + 1] for i, a in enumerate(args[:-1])
                                              if (a.startswith("-") and not a.startswith("--") or i == 0)
                                              and a.endswith("f")]
    return out


_GIT_WORKTREE_WRITES = {"pull", "checkout", "switch", "reset", "merge", "rebase", "restore", "stash", "apply",
                        "am", "cherry-pick", "revert", "clean", "rm", "mv"}


_GIT_EXEC_KEYS = lazy(r"(?i)^(core\.(pager|editor|fsmonitor|sshcommand|askpass|gitproxy|alternaterefscommand)|"
                            r"pager\..+|sequence\.editor|diff\.external|diff\..+\.(command|textconv)|merge\..+\.driver|"
                            r"filter\..+\.(clean|smudge|process)|credential\..*helper|gpg\.program|gpg\..+\.program|"
                            r"interactive\.difffilter|uploadpack\.packobjectshook|remote\..+\.(uploadpack|receivepack)|"
                            r"(mergetool|difftool|browser|man)\..+\.(cmd|path)|trailer\..+\.command|"
                            r"submodule\..+\.update|sendemail\.(sendmailcmd|smtpserver))$")
_GIT_VALUED = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env")  # global options taking a value
_GIT_EXEC_ENV = {"GIT_PAGER", "PAGER", "GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_SSH", "GIT_EDITOR", "EDITOR", "VISUAL",
                 "GIT_SEQUENCE_EDITOR", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND"}


_GIT_CONFIG_SOURCES = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG", "GIT_TEMPLATE_DIR", "GIT_EXEC_PATH"}
_GIT_FILE_ENV = lazy(r"^(GIT_TRACE\w*|GIT_INDEX_FILE)$")  # a path value: git writes that file
# T-0270: options whose value is a command git runs (a short flag's value may also be attached: -O'cmd')
_GIT_EXEC_FLAGS = {"rebase": ("-x", "--exec"), "difftool": ("-x", "--extcmd"), "grep": ("-O", "--open-files-in-pager"),
                   "fetch": ("--upload-pack",), "pull": ("--upload-pack",), "clone": ("--upload-pack", "-u"),
                   "ls-remote": ("--upload-pack",), "fetch-pack": ("--upload-pack", "--exec"), "archive": ("--exec",),
                   "push": ("--receive-pack", "--exec"), "send-pack": ("--receive-pack", "--exec"),
                   "filter-branch": ("--tree-filter", "--index-filter", "--msg-filter", "--env-filter", "--commit-filter",
                                     "--parent-filter", "--tag-name-filter")}
_GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--unset", "--unset-all",
                     "--remove-section", "--rename-section", "--show-origin", "--show-scope", "--edit", "-e"}


def _git_writes(sub, rest):
    """T-0270: the files and folders a git subcommand writes, relative to where it runs."""
    if sub == "format-patch":
        return _opt_values(rest, "-o", "--output-directory")
    if sub == "archive":
        return _opt_values(rest, "-o")
    if sub == "config":
        return _opt_values(rest, "-f", "--file")
    if sub == "checkout-index":
        return _opt_values(rest, "--prefix")
    if sub == "bundle" and rest[:1] == ["create"]:
        return _git_pos(rest[1:], ())[:1]
    if sub == "worktree" and rest[:1] == ["add"]:
        return _git_pos(rest[1:], ("-b", "-B", "--reason"))[:1]
    if sub == "worktree" and rest[:1] in (["remove"], ["move"]):  # T-4340 review: the folder it deletes or moves is a write
        return _git_pos(rest[1:], ("--reason",))[:2]
    if sub == "init":
        return _git_pos(rest, ("--template", "-b", "--initial-branch", "--object-format", "--ref-format",
                               "--separate-git-dir"))[:1] or ["."]
    if sub in ("fast-export", "fast-import"):
        return _opt_values(rest, "--export-marks")
    if sub in ("index-pack", "bugreport", "diagnose"):
        return _opt_values(rest, "-o", "--output-directory")
    if sub == "pack-objects" and "--stdout" not in rest:
        return _git_pos(rest, ())[:1]
    if sub == "submodule" and rest[:1] == ["add"]:
        pos = _git_pos(rest[1:], ("-b", "--branch", "--name", "--reference", "--depth"))
        if pos:  # <repo> [<path>]; without a path, the repo's name
            return pos[1:2] or [re.sub(r"\.git$", "", pos[0].rstrip("/").rsplit("/", 1)[-1])]
    return []


def _git_pos(args, valued):
    """Positionals of a git subcommand's arguments, without the values of the options in `valued`."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in valued:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


_ASSIGN_ANY = lazy(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=(.*)$", re.S)  # NAME=v and NAME+=v


def _assignments(argv):
    """(NAME, value) for what a simple command sets: every assignment before its real command — wherever the
    wrappers and keywords _strip_wrappers skips put it (timeout 5 env X=…, do X=…, env -u H X=…) — and
    export/declare's arguments."""
    words = list(argv)
    stripped, _ = _strip_wrappers(words)
    cand = words[:len(words) - len(stripped)]
    if stripped and os.path.basename(stripped[0]) in ("export", "declare", "typeset", "readonly", "local"):
        cand += stripped[1:]
    return [m.groups() for m in map(_ASSIGN_ANY.match, cand) if m]


def _clip(s, n=60):
    return s if len(s) <= n else s[:n] + "…"


def _opts_match(rx):
    """A code-loading variable's check: its value is refused unread (None) when rx finds a loading option, else it is
    harmless (False)."""
    return lambda val: None if re.search(rx, val) else False


# T-0275: variables that make the next program run code. A value that is a command or a script is read (the string
# returned is checked as a command); one that names a binary or a module can't be read here: None refuses it.
_CODE_VARS = {
    "BASH_ENV": lambda v: f"source {shlex.quote(v)}", "ENV": lambda v: f"source {shlex.quote(v)}",  # ENV: sh only
    "PROMPT_COMMAND": lambda v: v, "LESSOPEN": lambda v: v.lstrip("|-") or False,
    "LESSCLOSE": lambda v: v.lstrip("|-") or False, "RSYNC_RSH": lambda v: v,
    "TAR_OPTIONS": lambda v: next((c for _, c in _exec_options("tar", _words(v)) if c is not None), False),
    "LD_PRELOAD": lambda v: None, "LD_AUDIT": lambda v: None,
    "NODE_OPTIONS": _opts_match(r"(?:^|\s)(?:-r\b|--require\b|--import\b|--loader\b|--experimental-loader\b)"),
    "PERL5OPT": _opts_match(r"(?:^|\s)-[Mm]"), "RUBYOPT": _opts_match(r"(?:^|\s)-r"),
    **{k: _opts_match(r"-(?:javaagent|agentpath|agentlib|Xbootclasspath|XX:OnOutOfMemoryError|XX:OnError)")
       for k in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")},
    **{k: _opts_match(r"\$\(|`") for k in ("PS4", "SHELLOPTS")},  # expanded by the shell on every traced line
}


def _words(s):
    try:
        return shlex.split(s)
    except ValueError:
        return s.split()


def _parse_opts(args, short="", long=()):
    """([(option, value)], positionals) of an argv: a letter in `short` takes the rest of its cluster, else the next
    word (-avze CMD, -Co OPT, -I'cmd'); a `long` option takes =value or the next word. Everything after -- is positional."""
    vals, pos, i = [], [], 0
    while i < len(args):
        a, i = args[i], i + 1
        if a == "--":
            pos += args[i:]
            break
        if a.startswith("--"):
            key, eq, v = a.partition("=")
            if key in long:
                if not eq and i < len(args):
                    v, i = args[i], i + 1
                vals.append((key, v))
        elif a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], 1):
                if ch in short:
                    v = a[j + 1:]
                    if not v and i < len(args):
                        v, i = args[i], i + 1
                    vals.append(("-" + ch, v))
                    break
        else:
            pos.append(a)
    return vals, pos


def _ssh_options(name, args):
    out = []
    vals, _ = _parse_opts(args, "BbcDEeFIiJLlmOopQRSWw" if name == "ssh" else "BbcDFiJloPRSsX")
    for opt, o in vals:
        if opt == "-o":
            m = re.match(r"\s*(\w+)\s*[=\s]\s*(.*)", o, re.S)  # Key=value and "Key value"
            key, val = (m.group(1).lower(), m.group(2).strip().strip("'\"")) if m else ("", "")
            if key in ("proxycommand", "localcommand", "knownhostscommand") and val:
                out.append((f"ssh -o {m.group(1)}", val))
            elif key in ("pkcs11provider", "securitykeyprovider") and val not in ("", "none"):
                out.append((f"ssh -o {m.group(1)}={_clip(val)} loads a library", None))
        elif opt == "-F" and o != "/dev/null":  # a config file may hold a ProxyCommand or LocalCommand
            out.append(("ssh -F config file (it may set a ProxyCommand or LocalCommand)", None))
        elif opt == "-I" and name == "ssh" and o:
            out.append((f"ssh -I {_clip(o)} loads a PKCS#11 library", None))
        elif opt == "-S" and name != "ssh" and o:  # scp/sftp -S PROGRAM replaces ssh
            out.append((f"{name} -S", o))
    return out


def _sed_code(args):
    """What sed's `e` command and `s///e` flag run: the `e COMMAND` text, or None when it is computed from the input."""
    if "--sandbox" in args:
        return []
    args = [next((f + a[len(a.partition("=")[0]):] for f in ("--expression", "--file")
                  if len(a.partition("=")[0]) >= 4 and f.startswith(a.partition("=")[0])), a)
            if a.startswith("--") else a for a in args]
    vals, pos = _parse_opts(args, "efl", ("--expression", "--file"))
    out = [("sed -f/--file runs a script file the guard can't read here", None)
           for o, _ in vals if o in ("-f", "--file")][:1]
    scripts = [v for o, v in vals if o in ("-e", "--expression")] or pos[:1]
    addr = re.compile(r"\s*(?:\d+|\$|[,~+!]|/(?:\\.|[^/\\])*/[IM]*|\\(.)(?:\\.|(?!\1).)*?\1[IM]*)")
    for text in scripts:
        unsplit = False
        for seg in re.split(r";|\n", text):
            while True:  # addresses, and the { of a block, before the command letter
                n = 0
                while (am := addr.match(seg, n)) and am.end() > n:
                    n = am.end()
                seg = seg[n:].lstrip()
                if not seg.startswith("{"):
                    break
                seg = seg[1:]
            seg = seg.rstrip().rstrip("}").rstrip()
            if seg[:1] and not seg[0].isalpha() and seg[0] not in "#=:}":
                unsplit = True  # an address form the guard doesn't parse
            m = re.match(r"e(?:\s+(.*))?$", seg)
            if m:
                out.append(("sed e command", m.group(1) or None))
            elif seg.startswith("s") and len(seg) > 1:
                d = re.escape(seg[1])
                if re.match(rf"s{d}(?:\\.|[^{d}\\])*{d}(?:\\.|[^{d}\\])*{d}\w*e", seg):
                    out.append(("sed s///e runs its result as a command", None))
                elif not re.fullmatch(rf"s{d}(?:\\.|[^{d}\\])*{d}(?:\\.|[^{d}\\])*{d}[\w\s}}]*", seg):
                    unsplit = True  # a ; (or newline) inside the s command cut it: the split is a guess
        if unsplit and re.search(r"[/|#,:@!]e\b|(?:^|[;\n{}\s])e(?:\s|$)", text):
            out.append(("sed script with an e command or flag the guard can't split reliably", None))
    return out


def _vim_code(args):
    """-S/-s/-u/-l/--cmd load a script the guard can't read; in a -c or + command a `!` (after a space, an address, r
    or read; not wq!) runs the rest in the shell, and :py/:lua/system() run code."""
    out = []
    i = 0
    while i < len(args):
        a, i = args[i], i + 1
        nxt = args[i] if i < len(args) else ""
        if a.startswith("-S") or a in ("-s", "-l", "--cmd") or a == "-u" and nxt not in ("NONE", "NORC", "DEFAULTS"):
            out.append((f"vim {a} loads a script the guard can't read", None))
            continue
        cmd = nxt if a == "-c" else a[2:] if a.startswith("-c") and len(a) > 2 else a[1:] if a.startswith("+") else None
        if not cmd:
            continue
        bangs = [x.rstrip("\"' ") for x in re.findall(r"(?:^|[\s:|%,;\"'$\d]|\br|\bread)!\s*(\S.*)$", cmd)]
        out += [("vim !command", x) for x in bangs if x]
        if not bangs and re.search(r"(?:^|[\s:|])(?:system\(|py3?\b|python3?\b|pyx\b|lua\b|perl\b|ruby\b|terminal\b)", cmd):
            out.append(("vim command that runs code", None))
    return out


def _exec_options(name, args):
    """T-0275: [(why, shell code or None)] for the command string an option makes a program run (tar --to-command,
    ssh -o ProxyCommand, rsync -e, zip -TT, less +!cmd, vim -c !cmd, sed e). None: refused, it can't be read here.
    Long options match by prefix as GNU getopt does (--to-com= is --to-command); a prefix of two exec options
    expands to the first."""
    out = []
    longs = {"tar": ("--to-command", "--use-compress-program", "--info-script", "--new-volume-script",
                     "--checkpoint-action", "--rsh-command", "--rmt-command"), "rsync": ("--rsh",),
             "zip": ("--unzip-command",)}.get({"gtar": "tar", "bsdtar": "tar"}.get(name, name), ())
    if longs:
        args = [next((f + a[len(a.partition("=")[0]):] for f in longs if f.startswith(a.partition("=")[0])
                      and len(a.partition("=")[0]) >= 5 and a.partition("=")[0] != "--checkpoint"), a)
                if a.startswith("--") else a for a in args]
    if name in ("tar", "gtar", "bsdtar"):
        vals, _ = _parse_opts(args, "bCfFgHIKLNTVX", longs)
        for opt, v in vals:
            if opt == "--checkpoint-action":
                out += [("tar --checkpoint-action exec", v[5:])] if v.startswith("exec=") and v[5:].strip() else []
            elif opt in ("-I", "-F") or opt in longs:
                out += [(f"tar {opt}", v)] if v.strip() else []
    elif name in ("ssh", "scp", "sftp"):
        out += _ssh_options(name, args)
    elif name == "rsync":
        vals, _ = _parse_opts(args, "eBTfM@", ("--rsh",))
        out += [("rsync -e", v) for o, v in vals if o in ("-e", "--rsh") and v.strip()]
    elif name == "zip":
        out += [("zip -TT", v) for v in _opt_values(args, "-TT", "--unzip-command") if v.strip()]
    elif name == "less":
        out += [("less +!", a[2:]) for a in args if a.startswith("+!")]
    elif name in ("vim", "vi", "nvim", "view", "ex", "gvim"):
        out += _vim_code(args)
    elif name in ("sed", "gsed"):
        out += _sed_code(args)
    elif name in ("cargo", "rustc"):  # T-3607 review: config, or rustc arguments, from a file the guard can't read
        n = args.index("--") if "--" in args else len(args)
        configs = _opt_values(args[:n], "--config") if name == "cargo" else []
        out += [("cargo --config FILE loads config the guard can't read here", None)
                for v in configs if not _CARGO_INLINE.match(v)][:1]
        out += [("cargo --config with rustc @FILE arguments the guard can't read here", None)
                for v in configs if any(s.lstrip().startswith("@") for s in _strings(v))][:1]
        out += [("rustc @FILE reads arguments the guard can't read here", None) for a in (
            args if name == "rustc" else args[n + 1:] if "rustc" in args[:n] else []) if a.startswith("@")][:1]
    return out


_SCRIPT_LIMIT = 1 << 18
_SH_NAME = lazy(r"^(ba|z|da|k|fi|c|tc)?sh$")


_ENV_LONG = {"ignore-environment": 0, "null": 0, "debug": 0, "split-string": 1, "unset": 1, "chdir": 1, "argv0": 1,
             "default-path": 1, "block-signal": 0, "default-signal": 0, "ignore-signal": 0,
             "list-signal-handling": 0, "version": 0, "help": 0}


def _shebang_interp(head):
    """The interpreter's basename a '#!' line names (see _shebang)."""
    return os.path.basename(_shebang(head)[0])


def _shebang(head):
    """(interpreter word, command) of a '#!' line: the command is what the kernel runs, interpreter onward. For env,
    the first word after its own options (-S, -i, -u NAME, -C DIR, long options by unique prefix, ...) and NAME=value
    words. An env line that names no interpreter, or an option not recognised, reads as 'sh' (fail closed: the
    file is read as shell, never skipped). ("", "") when there is no #! line."""
    raw = head[2:].rstrip("\r\n") if head.startswith("#!") else ""
    ms = list(re.finditer(r"[^ \t]+", raw))
    if not ms:
        return "", ""
    if os.path.basename(ms[0].group()) != "env":
        return ms[0].group(), raw.strip(" \t")
    toks, ends, split = [m.group() for m in ms[1:]], [m.end() for m in ms[1:]], False
    i = 0
    while i < len(toks):
        w, after = toks[i], (raw[ends[i]:] if ends else " ".join(toks[i + 1:]))
        i += 1
        if w.startswith("--") and len(w) > 2:
            name, eq, val = w.partition("=")
            hits = [n for n in _ENV_LONG if n.startswith(name[2:])]
            if name[2:] in _ENV_LONG:
                hits = [name[2:]]
            if len(hits) != 1:  # unknown or ambiguous: don't guess which word is the program
                return "sh", ""
            if hits[0] == "split-string":
                toks, ends, i, split = _env_s_words(val + after if eq else after), [], 0, True
            elif _ENV_LONG[hits[0]] and not eq:
                i += 1
        elif w.startswith("-") and len(w) > 1 and w != "--":
            for j, c in enumerate(w[1:], 2):
                if c in "uCPaS":
                    if c == "S":
                        toks, ends, i, split = _env_s_words(w[j:] + after), [], 0, True
                    elif j == len(w):
                        i += 1
                    break
        elif w != "--" and w != "-" and not re.match(r"[^=\s]+=", w):
            word = (w.split() or ["sh"])[0].strip("'\"")  # a quoted "python3 -I" word: python3
            return word, shlex.join(toks[i - 1:]) if split else " ".join(toks[i - 1:])
    return "sh", ""  # env named nothing we could read: read the file as shell, never skip it


def _env_s_words(s):
    """env -S's string cut into words, quotes honoured; unbalanced quotes split on blanks."""
    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def _chmodded(path, chmodded):
    """True if an earlier command in this line gave path an exec bit ('*': a glob did; 'dir/': chmod -R did)."""
    return path in chmodded or "*" in chmodded or any(e.endswith("/") and path.startswith(e) for e in chmodded)


def _shebang_kind(data):
    """'py' or 'sh' for an executable's bytes, None for a binary. A python-like interpreter (an explicit path only if
    it resolves to one: a /tmp/python3 link to sh is a shell) reads as python; uv only as `uv run [options]`; any other
    text shebang, and a text file with no #! at all (bash runs it as a script: ENOEXEC), reads as shell."""
    if data.startswith(b"\x7fELF") or b"\0" in data:
        return None
    word, cmd = _shebang(data[:256].split(b"\n")[0].decode("utf-8", "replace"))
    if not word:
        return "sh"
    real = os.path.basename(os.path.realpath(word)) if "/" in word else os.path.basename(word)
    if os.path.basename(word) == "uv" and real == "uv":
        rest = cmd.split()[1:]
        return "py" if rest[:1] == ["run"] and all(w.startswith("-") for w in rest[1:]) else "sh"
    name = os.path.basename(word)
    if re.fullmatch(r"(?:i?python|pypy|py)[0-9.]*", real) and re.fullmatch(r"(?:i?python|pypy|py)[0-9.]*", name):
        return "py"
    if all(re.fullmatch(_INTERP_NAMES + r"|[gmn]?awk|osascript|java", x) for x in (real, name)):
        return None  # perl, node, ... : not shell, so reading it as shell would only invent denials (it stays unread)
    return "sh"


def _script_text(path, cwd, shebang=False, data=None, assume_x=False):
    """(text, problem, kind) of a script file a shell, source or python is told to run (shebang: of an executable run
    by its path, read only when it starts #! with a shell or python; a binary or other script is no concern).
    text None: not read (computed, missing, not a plain file). problem: refused (over the size limit).
    data: the file's bytes (at most limit + 1) the caller already read and hashed: the text is those bytes, never a
    second open that a swap could answer differently.
    ponytail: a script a variable names is not read; one over 256 KB is refused, run by path it is skipped."""
    if re.search(r"[$`*?\[]", path):
        return None, None, None
    p = os.path.join(cwd, os.path.expanduser(path))
    try:
        if not os.path.isfile(p):
            return None, None, None
        kind = None
        if shebang:
            if not (assume_x or os.access(p, os.X_OK)):
                return None, None, None
            if data is None:
                with open(p, "rb") as f:
                    data = f.read(_SCRIPT_LIMIT + 1)
            kind = _shebang_kind(data)
            if kind is None:
                return None, None, None
        size = len(data) if data is not None and len(data) <= _SCRIPT_LIMIT else os.path.getsize(p)
        if size > _SCRIPT_LIMIT:
            if shebang:
                return None, None, None
            return None, f"{path} is {size // 1024} KB, over the {_SCRIPT_LIMIT >> 10} KB the guard reads: run its " \
                         f"commands directly or split it", kind
        if data is not None:
            return data.decode("utf-8", "replace"), None, kind
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(), None, kind
    except OSError:
        return None, None, None


def _script_chunks(text, limit=2000):
    """A script's text cut at blank lines into pieces check_bash reads on their own: reading it whole is quadratic
    (a 21 KB deploy script took 12 s). A piece stays open while a quote or a heredoc is open in it; a cut never
    drops text. A generator: the quotes of a piece are read when the piece is wanted, so a read cut off by its time budget
    has not spent it on the text past the cut (a removal on line 1 of a long script is judged first)."""
    cur = []
    lex = _QuoteLexer()  # T-4758: where a quote is open as the shell reads it
    for line in text.split("\n"):
        cur.append(line)
        lex.feed(line)
        chunk = "\n".join(cur)
        if len(chunk) < limit // 4:
            continue
        open_here = any(not re.search(rf"^\s*{re.escape(m.group(1))}\s*$", chunk[m.end():], re.M)
                        for m in re.finditer(r"<<-?\s*['\"]?(\w+)['\"]?", chunk))
        # a count of quotes is wrong for what a line holds that isn't one (`"it's"`, `\``, a # comment), and a piece
        # that no cut ends costs more than its size grows (wg-quick: one 11 KB piece, 25 s): where the quotes are
        # even, or the reading of them has none open, is a place to cut
        balanced = chunk.count("'") % 2 == 0 and chunk.count('"') % 2 == 0 and chunk.count("`") % 2 == 0 \
            and chunk.count("$(") <= chunk.count(")")
        if not open_here and (balanced or lex.closed()) and not chunk.rstrip().endswith("\\"):
            yield chunk
            cur = []
    if cur:
        yield "\n".join(cur)


class _QuoteLexer:
    """Whether the lines fed so far end outside every quote, `…` and $( … ), as _strip_comments reads them (a # starts
    a comment at the start of a word, each substitution is a fresh context), one pass over the script. A heredoc's
    body is not read: its apostrophes are text. ponytail: a case pattern's `)` inside a $( … ) is read as its end."""

    def __init__(self):
        self.st, self.esc, self.docs = [["", None, 0]], False, []  # contexts [kind, quote, paren depth]; heredoc ends

    def closed(self):
        return len(self.st) == 1 and self.st[0][1] is None and not self.esc and not self.docs

    def feed(self, line):
        if self.docs:
            if re.match(rf"^\s*{re.escape(self.docs[0])}\s*$", line):
                self.docs.pop(0)
            return
        st, s, i = self.st, line + "\n", 0
        while i < len(s):
            ch, top = s[i], st[-1]
            if self.esc:
                self.esc = False
            elif ch == "\\" and top[1] != "'":
                self.esc = True
            elif top[1] == "'":
                top[1] = None if ch == "'" else "'"
            elif top[1] == "A":  # $'…': a backslash escapes (above), a ' ends it
                top[1] = None if ch == "'" else "A"
            elif ch == "`":
                if top[0] == "`" and top[1] is None:
                    st.pop()
                else:
                    st.append(["`", None, 0])
            elif ch == "$" and s[i + 1:i + 2] == "(":
                st.append(["$(", None, 0])
                i += 2
                continue
            elif ch == "$" and s[i + 1:i + 2] == "'" and top[1] is None:
                top[1] = "A"
                i += 2
                continue
            elif top[1] == '"':
                top[1] = None if ch == '"' else '"'
            elif ch in "'\"":
                top[1] = ch
            elif ch == "#" and (i == 0 or s[i - 1] in " \t\n;&|(<>"):
                break
            elif ch == "(" and top[0] == "$(":
                top[2] += 1
            elif ch == ")" and top[0] == "$(":
                if top[2]:
                    top[2] -= 1
                else:
                    st.pop()
            i += 1
        if len(st) == 1 and st[0][1] is None:  # a heredoc operator in a quote or a comment is not one
            self.docs += [m.group(1) for m in re.finditer(r"<<-?\s*['\"]?(\w+)['\"]?", line)]


_NAMED = lazy(r"^\S+ (?:line|from line) \d+: ")  # a finding a script read has already named its script and line for


def _read_piece(src, ctx, depth):
    """check_bash over one script piece (with the carried lines in front, if the caller put them there), reading
    each piece's own (text, folder) pairs against the cap of 256 and keeping no guessed target."""
    outer_seen = _DCWD.get("seen")
    _NO_GUESS.append(1)  # T-1778: a guessed target's finding names it ($, "not known"): dropped by the caller, so not made
    # T-4758: each piece counts its own (text, folder) pairs against the cap of 256 (check_bash), as a command of that
    # size would: a script's functions each name a folder or two, and 64 of them (37 KB) ran past it as one reading
    _DCWD["seen"] = {}
    try:
        return _child_check(src, ctx, depth + 1)  # T-4785: a script file runs in a child
    finally:
        _NO_GUESS.pop()
        if outer_seen is None:
            _DCWD.pop("seen", None)
        else:
            _DCWD["seen"] = outer_seen


def _named_finding(f, chunk, first, path, ctx, depth):
    """T-4774 review: a pipe-shell finding from a script names the script and the line, so the agent need not search:
    `PATH line N: <the typed form's message>`. The line's text is never quoted: a download line can carry a token or an
    authorization header. The line is the one that gives the finding read alone; a finding no single line gives (a
    pipeline broken by a continuation) names the piece's first line instead."""
    if not path or _NAMED.match(f[1]):  # one from a script further down the chain already says where
        return f
    for n, line in enumerate(chunk.split("\n")):
        if line.strip() and not line.lstrip().startswith("#") and f in _read_piece(line, ctx, depth):
            return (f[0], f"{path} line {first + n}: {f[1]}")
    return (f[0], f"{path} from line {first}: {f[1]}")


def _check_script(text, ctx, depth, acc, path=None):
    """check_bash over a script file's pieces, keeping what is certain: a target a variable holds ("not known before
    it runs", `$HOME/x`) is every script's `rm -rf "$DIR"`, and one that reads .env is a deploy script: counting them
    refused 4 of 10 real scripts. ponytail: a literal danger (rm -rf ~, a pipe into a shell) is what is caught.
    What it finds goes into `acc` as it goes, so a read cut off by the time budget keeps what it had."""
    # only what no script of the user's does innocently: a deploy script reboots, installs plugins, reads .env, and
    # splits words on IFS (replay of 3 real scripts: reboot, plugin, system and credentials refusals were all benign)
    state = []  # what earlier pieces did that a later one depends on: cd, literal assignments, files written
    first = 1  # the script's line number of the piece's first line
    # T-4811: `w=$(mktemp -d)` is the folder it is, as in a typed command (_mktemp_literal runs at depth 0 only, and a
    # piece is read at depth 1): a literal in the piece and in the lines carried on, so a later piece's `rm -rf "$w"` or
    # `trap 'rm -rf "$w"' EXIT` reads a temp folder, not an unknown name. Not where the script moves TMPDIR or PATH.
    literal = _mktemp_literal if not _MKTEMP_MOVED.search(text) else (lambda s: s)
    for chunk in _script_chunks(text):
        read = literal(chunk)
        # core: a script of Foreman's own (evals) writes its checkout; only its state folder counts
        got = _read_piece("\n".join(_carried(state, read) + [read]), ctx, depth)
        # T-4774 review: the carried lines (a `curl … > f` of an earlier piece) are in front of this piece, and a
        # download anywhere in the text it reads makes a pipe into a shell "downloaded": a pipe-shell finding is the
        # piece's own, read without them (a pipeline never straddles two pieces). A finding from a nested script is
        # already named with its own path and may need the carried state (a `cd` finds it): it is kept as read.
        # Not caught, by this choice: `curl x > f` in an earlier piece, then `cat f | sh` far later (typed, refused)
        if state and any(f[0] == "pipe-shell" and not _NAMED.match(f[1]) for f in got):
            got = [f for f in got if f[0] != "pipe-shell" or _NAMED.match(f[1])] + [
                f for f in _read_piece(chunk, ctx, depth) if f[0] == "pipe-shell"]
        deep = False
        for f in got:
            if f == ("system", "script nests too deep to read") or f[0] == "system" and f[1].endswith(_DEEP):
                pass  # from a script further down the chain: it keeps its way up (T-1931 review)
            elif f[1] == "command nesting too deep to analyse: split the command":  # T-1936: exact, not any "too deep"
                # a chain of scripts this deep hides its last one: refuse, never read as clean. Only from the third
                # script in a chain: a script of its own deep $( … ) nesting is no chain (5 of 330 real scripts)
                if len(_IN_SCRIPT) < 3:
                    deep = True
                    continue
                f = ("system", "script nests too deep to read")
            # T-4774: pipe-shell is every one a download the line's own words feed to a shell (curl … | sh, sh -c "$(curl
            # …)", eval, <(curl …)): the same finding and message as the typed form, certain in script text, and a
            # name a variable holds ("| $SH") carries a $, so the rule below drops it
            elif not ((f[0] in ("rm-outside", "self-authorize", "state-direct", "pipe-shell")
                       or f[0] == "core" and "/state" in f[1]
                       or f[0] == "confine" and f[1].startswith("sampo check")  # T-4785: a builder's script changes no gate
                       or f[0] == "system" and ("is written earlier in this command" in f[1] or "too deep" in f[1]))
                      and "$" not in f[1] and not re.search(r"not known|unresolvable|from stdin", f[1])):
                continue
            if f[0] == "pipe-shell":
                f = _named_finding(f, chunk, first, path, ctx, depth)
            if f not in acc:  # the carried lines are read again with each piece
                acc.append(f)
        # T-1931: the part past the cap was never read: scan it for literals, as a script too long to parse.
        # ponytail: literals only (rm -rf ~, a pipe into a shell, a state write): a core write that deep goes unseen
        if deep:
            acc += [f for f in dict.fromkeys(("system", f"{line[:80]} ({why}; {_DEEP}")
                                             for _n, line, why in _literal_scan(chunk, ctx)) if f not in acc]
        state += _state_lines(read)
        first += chunk.count("\n") + 1
    return acc


_STATE_LINE = lazy(r"^\s*(?:(?:cd|pushd|popd)\b.*|(?:(?:export|declare|local|readonly)\s+)?[A-Za-z_]\w*=[^\s`$(]*"
                         r"|.*(?:[^<>&\d]|^)>>?\s*(?!/dev/null)[^\s&>|]\S*.*|.*\b(?:tee|cp|mv|install|ln)\b.*)$")
_HEREDOC_OP = lazy(r"<<-?\s*['\"]?\w+['\"]?")


def _state_lines(chunk):
    """The lines of a piece a later piece may depend on: a cd, a plain assignment, a write (heredoc operators cut, so a
    carried line never opens a heredoc). Pieces are read with these in front, so `cd /` in one and `rm -rf etc` in the
    next are read as one script, and so is a file written in one and run in the next. ponytail: the last 40 lines."""
    out = []
    for line in chunk.split("\n"):
        line = _HEREDOC_OP.sub("", line)
        if line.strip() and not line.lstrip().startswith("#") and len(line) < 400 and "<<" not in line \
                and not line.rstrip().endswith("\\") and _STATE_LINE.match(line):
            out.append(_inert_subst(line))
    return out


# (review: a name that read, mapfile, let, getopts, declare or (( … = )) takes from an expansion, as _NAME_COMPUTED reads
# them, without its quote masking: that cost a 3,566-line script 3 s)
_ANY_NAME = lazy(r"\$\{!|\beval\b|\b(?:declare|typeset|local|export|readonly)\s+-\w*[np]|\bprintf\s+-v|"
                 r"\b(?:read|mapfile|readarray|getopts|let|declare|typeset|local|export|readonly)\b[^;&|\n<>]*\s[\"']?\$|"
                 r"\(\(\s*(?:[^()]*,\s*)?\$\{?\w+\}?\s*(?:[-+*/%^|&]|<<|>>)?=(?!=)")
_SPECIAL = frozenset(("IFS", "HOME", "PATH", "CDPATH", "TMPDIR", "TMP", "TEMP", "PWD", "OLDPWD", "SHELL", "BASH_ENV", "ENV",
                      "PS4", "PROMPT_COMMAND", "LD_PRELOAD", "LD_LIBRARY_PATH", "GLOBIGNORE", "FIGNORE", "SHELLOPTS",
                      "BASHOPTS", "POSIXLY_CORRECT", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"))  # (review: carry them too)
_ASSIGN_ONLY = lazy(r"^\s*(?:(?:export|declare|local|readonly)\s+)?([A-Za-z_]\w*)=[^\s`$(]*$")


def _carried(state, text):
    """T-4811: the carried lines (the last 40) a piece's reading can use: every cd and write, and a plain assignment only
    when the piece names that variable (or the name changes how bash reads: IFS, HOME, PATH …), unless the piece reaches
    a name by computing it (`${!v}`, eval, a nameref, printf -v). 40 lines of `A_17=x` in front of every piece doubled
    its text and its read, and a long script of 260 pieces paid it each time."""
    last = state[-40:]
    if _ANY_NAME.search(text):
        return last
    names = set(re.findall(r"[A-Za-z_]\w*", text))
    return [line for line in last
            if not (m := _ASSIGN_ONLY.match(line)) or m.group(1) in names or m.group(1) in _SPECIAL]


_STATE_WORD = lazy(r"[<>=]|\b(?:cd|pushd|popd|tee|cp|mv|install|ln|eval|source|exec)\b|(?<![\w./-])\.(?=\s)")


def _inert_subst(line):
    """A carried line with each innermost `$( … )` and `…` that holds nothing a later piece depends on (no write, no cd,
    no assignment, no substitution inside) read as `$S` (T-4758): the piece it came from was read with the body, and
    every `$(` in the text is read again to its end, so n of them in the carried lines cost n^2 with each piece (24
    `for f in $(ls "$out")` lines: 3 KB, 2.8 s)."""
    prev = None
    while prev != line:
        prev = line
        line = re.sub(r"\$\(((?:[^()`$]|\$(?![(]))*)\)|`((?:[^`$]|\$(?![(]))*)`", _inert_body, line)
    return line


def _inert_body(m):
    body = m.group(1) if m.group(1) is not None else m.group(2)
    # the word that names the command stays as it was: a bare $S there reads as a name that may be a cd, and the
    # folders it might leave the carried lines in multiply (dtx_diff: `basename $0` ARCH=… >x, four lines, 256 pairs)
    before = re.sub(r"\S*$", "", m.string[:m.start()])
    if re.search(r"(?:^|[;&|({!]|\b(?:then|do|else|elif|if|while|until|time))\s*(?:\w+=\S*\s+)*$", before):
        return m.group(0)
    # a quote or backslash in it may hide a paren from the match (`$(echo ")")`), so only plain double quotes, in pairs
    body = re.sub(r"(?:&|\d?)>>?\s*/dev/null\b|\d?>&\d\b", "", body)  # a redirect to nowhere writes nothing
    if _STATE_WORD.search(body) or "'" in body or "\\" in body or body.count('"') % 2:
        return m.group(0)
    return "$S"


class _OverBudget(BaseException):  # not an Exception: check_bash's own handlers must not swallow it
    pass


_UNREAD = []  # (path, why) of scripts this call left unread: "system: …" allowed like a binary, "refused: …" blocked
_IN_SCRIPT = []  # non-empty while a script is being read: the outermost read owns the clock
_SYS_FS = {"stat": os.stat, "access": os.access, "euid": os.geteuid}  # the readers _system_script uses: a test swaps them


def take_unread():
    """The scripts this call could not read to the end, (path, why): the hook logs each as a guard_unread event (so the
    count of refusals is measurable) and notes the allowed ones."""
    out, _UNREAD[:] = list(_UNREAD), []
    return out


def _system_script(lex, real):
    """True when the script file `real` (the resolved path of `lex`, the path as named) is no more dangerous unread than a
    binary (T-4758): a regular file root owns, this user cannot write, in directories (along both paths: a link in a
    place this user writes could be pointed elsewhere) that root owns and this user cannot write. Root, not any other
    account: a second unprivileged account this user can reach could plant a long script in its own home.
    Any stat error is False: the exemption is refused on doubt."""
    import stat as st
    try:
        euid = _SYS_FS["euid"]()
        s = _SYS_FS["stat"](real)
        if not st.S_ISREG(s.st_mode) or s.st_uid != 0 or euid == 0 or _SYS_FS["access"](real, os.W_OK):
            return False
        for start in dict.fromkeys((lex, real)):
            d = os.path.dirname(start)
            while True:
                if _SYS_FS["stat"](d).st_uid != 0 or _SYS_FS["access"](d, os.W_OK):
                    return False
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
        return True
    except (OSError, ValueError):
        return False


_SCRIPT_CAP = 60.0  # CPU seconds: far above the 2 s hard read; a larger FM_GUARD_SCRIPT_* value is capped to it


def _env_seconds(name, default):
    """T-4824: a FM_GUARD_SCRIPT_* value in CPU seconds: a positive finite number, capped at _SCRIPT_CAP. Text, zero, a
    negative, nan or inf is the default, so the guard never raises on its own environment (-1 made setitimer raise
    ItimerError out of it) and every script read stays timed (nan left the SIGPROF handler installed, untimed)."""
    try:
        v = float(os.environ.get(name) or default)
    except ValueError:
        v = float("nan")
    if not math.isfinite(v) or v <= 0:
        v = float(default)
    return min(v, _SCRIPT_CAP)


def _script_budget():
    """The first read's CPU seconds (FM_GUARD_SCRIPT_BUDGET, see _env_seconds), default _BUDGET."""
    return _env_seconds("FM_GUARD_SCRIPT_BUDGET", _BUDGET)


_BUDGET = 0.2  # CPU seconds. 0.5 gave a cold hook p95 of 331 ms over 330 real scripts (goal 300); 0.2 trims the tail


def _script_hard():
    """The second read's CPU seconds (FM_GUARD_SCRIPT_HARD, see _env_seconds), 10x the first's by default (T-4758): a
    slow runner spends the first on a 220-line script, and the literal scan after it cannot see a relative target after
    a `cd /`."""
    return _env_seconds("FM_GUARD_SCRIPT_HARD", _script_budget() * 10)


_WRAPPER_WORDS = frozenset(("sudo", "doas", "command", "builtin", "exec", "time", "nohup", "nice", "then", "do", "else", "elif",
                            "if", "while", "until", "!"))
_UNFINISHED_VERBS = {"rm", "rmdir", "mv", "shred", "truncate", "unlink", "find", "chmod", "chown", "dd"}


def _unfinished_removal(text):
    """(line number, line, why) for each removal in a script read only by literals, after a `cd`/`pushd` that leaves the
    folder (an absolute or ~ path, `..`, or none), that names a relative target: what the cut-off read would have
    judged by the folder it stood in. Narrow on purpose: `cd build; rm -rf out`, a variable target and an absolute one
    (the literal scan's) stay allowed, as does a removal with no cd out before it (T-4758)."""
    out, left = [], False
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.split(" #", 1)[0] if not raw.lstrip().startswith("#") else ""
        for seg in re.split(r"[;&|\n(){}`]+", line):
            words = seg.split()
            i = 0  # an index, not words[1:] again and again: a run of `sudo` is linear
            while i < len(words) and (re.sub(r"[\\'\"]", "", words[i]) in _WRAPPER_WORDS or _ASSIGN.match(words[i])):
                i += 1
            if i >= len(words):
                continue
            verb = re.sub(r"[\\'\"]", "", words[i]).rsplit("/", 1)[-1]
            args = [re.sub(r"[\\'\"]", "", w) for w in words[i + 1:]]
            plain = [a for a in args if a != "--" and not (a.startswith("-") and len(a) > 1)]
            if verb in ("cd", "pushd"):
                t = plain[0] if plain else ""
                if t and "$" not in t and not t.startswith(("/", "~", "-")):
                    t = os.path.normpath(t)  # ./a/../.. leaves the folder, build/.. does not
                if not t or "$" not in t and t.startswith(("/", "~", "..", "-")):
                    left = True
            elif left and verb in _UNFINISHED_VERBS:
                if verb == "find":
                    hit = "-delete" in args and not any(a.startswith(("/", "~", "$")) for a in plain[:1])
                elif verb in ("chmod", "chown"):
                    hit = any(a in ("-R", "-r") or (a.startswith("-") and "R" in a) for a in args) \
                        and any(not a.startswith(("/", "~", "$", "-")) and "$" not in a for a in plain[1:])
                elif verb == "dd":
                    hit = any(a.startswith("of=") and not a[3:].startswith(("/", "~", "$", "&")) and "$" not in a for a in args)
                else:
                    hit = any(a and not a.startswith(("/", "~", "$")) and "$" not in a for a in plain)
                if hit:
                    out.append((n, raw.strip(), "a removal by a relative path after a cd out of the folder"))
    return out


def _script_cache_path(ctx):
    """In Sampo's state folder, where the guard refuses a direct write: a session can't forge an entry."""
    return os.path.join(ctx.state_dir or os.path.join(ctx.foreman_home, "state"), "guard-scripts.json")


_GUARD_HASH = []


def _guard_hash():
    """sha256 of this file's bytes: an edited guard reads every script again."""
    if not _GUARD_HASH:
        import hashlib
        with open(__file__, "rb") as f:
            _GUARD_HASH.append(hashlib.sha256(f.read()).hexdigest()[:16])
    return _GUARD_HASH[0]


_CACHE_MAX = 200


def _pairs(v, n):
    return isinstance(v, list) and all(isinstance(x, list) and len(x) == n and all(isinstance(y, str) for y in x)
                                       for x in v)


def _cache_load(path):
    """The cache's good entries: one that is malformed (a hand edit, a crash, an older shape) is dropped on its own,
    never an error that blocks every script run. Limit, documented not fixed: the cache is trusted as the guard's own
    file because the guard refuses a Bash write to Sampo's state; a write made outside the guard (another program
    running as the user) can forge an entry."""
    import json
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("p"), str)
            and _pairs(v.get("f"), 2) and _pairs(v.get("n", []), 2) and isinstance(v.get("u"), (str, type(None)))
            and isinstance(v.get("at"), (int, float)) and not isinstance(v.get("at"), bool)}


def _cache_store(path, key, entry):
    """Atomic write of one more entry: files that are gone dropped first, then the oldest past 200."""
    import json
    cache = {k: v for k, v in _cache_load(path).items() if isinstance(v, dict) and os.path.exists(v.get("p", ""))}
    cache[key] = entry
    for k in sorted(cache, key=lambda k: cache[k].get("at", 0))[:max(0, len(cache) - _CACHE_MAX)]:
        del cache[k]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError:
        pass  # no cache dir: the next run reads the file again


_SCAN = [(lazy(rx, re.M | re.I if ci else re.M), why) for rx, why, ci in (
    # every quantifier is bounded: a line of NODE_OPTIONS= repeated would otherwise cost its length per occurrence
    (r"\b(?:LD_PRELOAD|LD_AUDIT|BASH_ENV|PROMPT_COMMAND|RSYNC_RSH|PERL5OPT)=[^\s\n]", "a code-loading variable", False),
    (r"\b(?:NODE_OPTIONS|RUBYOPT)=[^\n]{0,200}?(?:--require|--import|--loader|(?:^|[ \t'\"=])-r)",
     "code loaded by an option", False),
    (r"(?:^|[\s;&|(])(?:(?:ba|z|da|k)?sh\b|source\b|\.(?=[ \t])|eval\b)[^\n]{0,60}?(?:<\(|\$\(|`)\s*(?:curl|wget)\b",
     "downloaded code run", False),
    (r"-javaagent|OnOutOfMemoryError|-XX:OnError|--to-command|--use-compress-program|--checkpoint-action=exec|"
     r"--rsh-command|--rmt-command|--unzip-command", "a command an option runs", False),
    (r"\b(?:Proxy|Local)Command[ =]|\bvim?[ \t][^\n]{0,200}?-c[ \t]*['\"]?!", "a command an option runs", True),
)]
_LINE_CAP = 4096  # a longer line is scanned in overlapping pieces of this size, never as one backtracking field
_RM_TARGET = lazy(r"/?\*?$")
_PIPE_SH = lazy(r"(?:curl|wget)\b[^|\n]*(?:\|[^|\n]*)*?\|\s*(?:(?:sudo|exec|env|command|nohup)(?:\s+-\S+)*\s+|"
                      r"[A-Za-z_]+=\S*\s+)*(?:/\S+/)?(?:ba|z|da|k)?sh\b")


def _scan_units(text):
    """(text, original line number per line): continuation lines joined (rm \\ -rf \\ ~), lines over _LINE_CAP cut
    into overlapping pieces so no regex runs over more than that at once."""
    out, nums, lines, i = [], [], text.split("\n"), 0
    while i < len(lines):
        start, cur = i + 1, lines[i]
        # (a comment ends at its newline whatever it ends with: bash runs the next line)
        while cur.endswith("\\") and not cur.lstrip().startswith("#") and i + 1 < len(lines) and len(cur) < 4 * _LINE_CAP:
            i += 1
            cur = cur[:-1] + " " + lines[i]
        step = _LINE_CAP * 7 // 8
        for k in range(0, max(len(cur), 1), step):
            out.append(cur[k:k + _LINE_CAP])
            nums.append(start)
            if k + _LINE_CAP >= len(cur):
                break
        i += 1
    return "\n".join(out), nums


def _commented(text, pos):
    """Whether the line holding pos is a comment (`#   curl … | sh` in a usage note isn't run)."""
    return text[text.rfind("\n", 0, pos) + 1:pos].lstrip().startswith("#")


def _rm_hit(text):
    """The first `rm` with a recursive flag and a target of ~, / or $HOME (also /*, "$HOME"/*), operands and flags in
    any order, as (position, line)."""
    for m in re.finditer(r"\brm\b([^\n;&|]*)", text):
        if _commented(text, m.start()):
            continue
        ops = m.group(1)[:_LINE_CAP].split()
        flags = [o for o in ops if o.startswith("-") and not o.startswith("--") and o[1:].isalpha()]
        if any("r" in f or "R" in f for f in flags) or "--recursive" in ops:
            for o in ops:
                t = re.sub(r"/{2,}", "/", o.replace('"', "").replace("'", ""))  # ~//* and // are ~/* and /
                if not t.startswith("-") and _RM_TARGET.sub("", t) in ("", "~", "$HOME", "${HOME}"):
                    return m.start(), m.group(0)
    return None


def _fm_hit(text):
    """The first `fm …` the guard itself refuses an agent (USER_ONLY_FM verbs, --allow core|remote), as (position, line):
    read from the guard's own tables, so a new entry there is caught here."""
    for m in re.finditer(r"\b" + FM_NAME_RE + r"\b([^\n;&|]*)", text):
        if _commented(text, m.start()):
            continue
        args = [a.replace('"', "").replace("'", "") for a in m.group(1)[:_LINE_CAP].split()]
        sub, rest = _fm_subcommand(args)
        if sub in USER_ONLY_FM and any(_user_only_hit(USER_ONLY_FM[sub][0], a) for a in rest):
            return m.start(), m.group(0)
        if any(_is_allow(a) and (a.partition("=")[2] or b) in USER_ONLY for a, b in zip(args, args[1:] + [""])):
            return m.start(), m.group(0)
    return None


def _pipe_sh_hit(text):
    """The first download piped (maybe through tee, env, a path, a newline) into a shell.
    ponytail: the first 200 curl/wget words and a 1500-character window each; a pipe further on is missed."""
    pos = 0
    for _ in range(200):
        m = re.compile(r"\b(?:curl|wget)\b").search(text, pos)
        if not m:
            return None
        h = _PIPE_SH.match(text, m.start(), m.start() + 1500)
        if h and not _commented(text, m.start()):
            return m.start(), h.group(0)
        pos = m.end()
    return None


def _literal_scan(text, ctx=None):
    """[(line number, line, why)] for the dangerous literals in a script's raw text: a plain regex pass with no shell
    parsing (about 10 ms for 256 KB). What the guard falls back to when a script can't be parsed in time."""
    out = []
    text, nums = _scan_units(text)
    at = lambda pos: nums[min(text.count("\n", 0, pos), len(nums) - 1)]  # noqa: E731  original line of a position
    for hit, why in ((_rm_hit(text), "rm -r of ~, / or $HOME"), (_pipe_sh_hit(text), "a download piped into a shell"),
                     (_fm_hit(text), "an sampo command only the user may run")):
        if hit:
            out.append((at(hit[0]), hit[1].strip()[:120], why))
    # a write into Foreman's state: find the path as a plain substring (a regex over the whole text was 30 ms), then
    # look for a writing word on that line
    marks = [".claude/foreman/state", ".claude/sampo/state", "FOREMAN_HOME}/state", "FOREMAN_HOME/state", "SAMPO_HOME}/state", "SAMPO_HOME/state"]
    if ctx is not None:  # the state folder this guard runs with, when it isn't under ~/.claude/foreman
        marks.append(ctx.state_dir or os.path.join(ctx.foreman_home, "state"))
    # a path, quote, backslash or backtick before the verb (/bin/rm, \rm, "rm", `rm`); not rm-rf-helper or install-manifest
    verbs = re.compile(r"(?:^|[\s;&|(`\"'\\])(?:[\w./-]{0,200}/)?(?:tee|cp|mv|rm|install|ln)[\"']?(?=\s)|\bsed[ \t]+-i")
    redirect = re.compile(r">>?[ \t]*[\"']?([^\s;&|\"']+)")  # the target of a redirect, not `>/dev/null` on the line
    for mark in marks:
        i = text.find(mark)
        while i != -1:
            a, b = text.rfind("\n", 0, i) + 1, text.find("\n", i)
            line = text[a:len(text) if b == -1 else b]
            if not line.lstrip().startswith("#") and (verbs.search(line) or any(
                    mark in r.group(1) for r in redirect.finditer(line))):
                out.append((at(i), line.strip(), "a write into Sampo's state"))
                break
            i = text.find(mark, len(text) if b == -1 else b)
    for rx, why in _SCAN:
        for m in rx.finditer(text):
            a, b = text.rfind("\n", 0, m.start()) + 1, text.find("\n", m.start())
            line = text[a:len(text) if b == -1 else b].strip()
            if line.startswith("#"):  # a usage comment ("#   curl … | sh") isn't run
                continue
            out.append((at(m.start()), line, why))
            break  # the first of each kind is enough to refuse
    return sorted(out)


_TEXTS = []  # (path, text) of the scripts read since the outermost read began: what a budget cut-off scans
_DEPS = []  # (path, sha256) of every nested script that read used: a cached verdict is valid while they are unchanged


def _file_digest(p):
    """sha256 of a script's bytes as the verdict read them (at most limit + 1), "" if it can't be read (absent too:
    a dependency on an absent script holds only while it stays absent). A link is followed."""
    import hashlib
    try:
        if not os.path.isfile(p):
            return ""
        with open(p, "rb") as f:
            return hashlib.sha256(f.read(_SCRIPT_LIMIT + 1)).hexdigest()
    except OSError:
        return ""


_RUN_CMD = lazy(r"(?:^|[;&|(\s`])(?:/[^\s;&|]*/)?(?:env[ \t]+)?(?:(?:ba|z|da|k)?sh|python3?|source|\.)(?=[ \t])"
                      r"([^\n;&|)<>]{0,300})", re.M)
_RUN_DIRECT = lazy(r"(?:^|[;&|(\s`])((?:\./|/)[^\s;&|)<>\"'$`]*(?:\.(?:sh|bash|zsh|ksh|py)\b|(?<=\./)[^\s;&|)<>\"'$`]+))",
                         re.M)


def _run_targets(text):
    """Script files a script's text runs by name: bash/sh/zsh/python/source/. X (also /bin/bash, env bash, options such
    as -e and -o pipefail before X) and a ./x or /abs/x.sh run directly. Literal words only (a $ or ` is skipped)."""
    out, text = [], text[:_SCRIPT_LIMIT]
    for m in _RUN_CMD.finditer(text):
        words, i = m.group(1).split(), 0
        while i < len(words):
            w = words[i]
            i += 1
            if w in ("-o", "+o", "-O", "+O", "-W", "-X"):
                i += 1
            elif not w.startswith(("-", "+")):
                w = w.strip("\"'")
                if w and not re.search(r"[$`]", w):
                    out.append(w)
                break
        if len(out) >= 40:
            break
    out += [m.group(1) for m in _RUN_DIRECT.finditer(text)][:40]
    return out


def _script_verdict(script, cwd, kind, ctx, depth, assume_x=False, given=None):
    """Findings for the script file a command runs, cached by the sha256 of its bytes (and of this guard file) in
    Sampo's state folder, where the guard refuses direct writes: a script run again costs a hash, not a parse (the
    hook runs before every Bash call: a 20 KB script took seconds to parse cold). One cold read has a time budget
    (FM_GUARD_SCRIPT_BUDGET, CPU seconds, default _BUDGET). Past it the script is read again with 10x the time
    (_script_hard); past that too it is refused as too long to read (T-4758), the literal scan (_literal_scan) of it
    and of the scripts it runs naming what it finds first; either is cached. One that no user could have written
    (_system_script: every file it touched is root's, in directories this user cannot write) is allowed
    unread past both budgets, like a binary, with the note; the exemption is checked again on a cache hit. The file's
    own size over the limit is refused (_script_text). given: the bytes the caller already read (at most limit + 1)
    and will run, judged in place of a second read of the path (T-4139: `--run-file` runs those exact bytes)."""
    if re.search(r"[$`*?\[]", script):
        return []
    p = os.path.realpath(os.path.join(cwd, os.path.expanduser(script)))
    import hashlib
    import signal
    import time
    lex = os.path.normpath(os.path.join(cwd, os.path.expanduser(script)))  # as named: a link is followed at each check
    nested = bool(_IN_SCRIPT)
    data = given
    if data is None and os.path.isfile(p):  # a FIFO or a device would block the open below, and the hook with it
        try:
            with open(p, "rb") as f:
                data = f.read(_SCRIPT_LIMIT + 1)
        except OSError:
            pass
    if nested:  # the outer verdict is only as good as this file's bytes, or as its absence
        _DEPS.append((lex, hashlib.sha256(data).hexdigest() if data is not None else ""))
    if data is None:
        return []
    digest = hashlib.sha256(data).hexdigest()
    key = None
    if len(data) <= _SCRIPT_LIMIT:  # over it: refused below, nothing to cache
        key = "|".join((p, digest, hashlib.sha256(repr(
            (ctx.home, ctx.foreman_home, ctx.project_root, ctx.cwd, cwd, depth, tuple(ctx.scratch), ctx.state_dir, tuple(ctx.state_fallbacks), ctx.confine,
             ctx.unbriefed, tuple(getattr(ctx, "briefed_lanes", ())), kind, assume_x, _guard_hash())).encode()).hexdigest()[:16]))
    path = _script_cache_path(ctx)
    hit = None if nested or key is None else _cache_load(path).get(key)
    if hit and all(_file_digest(dp) == dd for dp, dd in hit.get("n", [])) \
            and all(_system_script(a, b) for a, b in hit.get("s", [])):  # the scripts it read, unchanged; still system files
        if hit.get("u"):
            _UNREAD.append((p, hit["u"]))
        return [tuple(f) for f in hit["f"]]
    found, unread, system = [], None, []
    if not nested:
        _TEXTS.clear()
        _DEPS.clear()
    _IN_SCRIPT.append(1)
    old, live = None, [False]
    try:
        text, why, got = _script_text(script, cwd, shebang=kind == "exec", data=data, assume_x=assume_x)
        if text is not None:
            _TEXTS.append((p, text))
            if kind == "exec":  # the #! line is a command too: env -S bash -c '…' runs its -c string, not the body
                found += _child_check(_shebang(data[:256].split(b"\n")[0].decode("utf-8", "replace"))[1], ctx, depth + 1)
        pre = list(found)  # `pre` and `read` are bound before the timer is set: one that fires at once (macOS, T-4820)
        #                    would otherwise reach the handler below with neither

        def read():
            if why:
                found.append(("system", why))
            elif text is not None and (got or kind) == "py":
                for spawned in _spawned_commands(text):  # only a command written out: computed ones ("$_")
                    if '"$_"' not in spawned:  # flag every demo script that builds a path (replay: 27 false denials)
                        found.extend(_child_check(spawned, ctx, depth + 1))
            elif text is not None:
                _check_script(text, ctx, depth, found, path=p)
        if not nested:
            def over(*_):
                if live[0]:  # T-4820: a fire after the last read ended (the scan, the cleanup) is not an over-budget read
                    raise _OverBudget()
            try:
                live[0] = True
                old = signal.signal(signal.SIGPROF, over)  # CPU time, not wall: a busy machine mustn't trip it
                signal.setitimer(signal.ITIMER_PROF, _script_budget())
            except ValueError:  # not the main thread: no clock (ponytail: only the main thread is time-boxed)
                old = None
        read()
        live[0] = False  # the read ended in time: the timer, still armed, is now harmless and is cancelled below
        if old is not None:
            signal.setitimer(signal.ITIMER_PROF, 0)
    except _OverBudget:
        if nested:
            raise  # the outermost read owns the clock and decides
        finished = too_long = False
        if old is not None:
            # T-4758: a slow machine spends the budget on a short script, and the literal scan below cannot judge what
            # the cut-off read never reached (a relative target after `cd /`, a file `bash X` runs, a word split across
            # a variable): every script past the first budget is read again, to the end, with 10x the time. One not
            # read to the end then is refused, whatever words it holds: a rule keyed on words is a denylist.
            first = list(found)  # what the cut-off read had found: kept if the second one is cut off too
            found[:] = pre
            try:
                signal.setitimer(signal.ITIMER_PROF, _script_hard())
                read()
                finished = True
            except _OverBudget:
                found[:] = first + [f for f in found if f not in first]
                too_long = True
            live[0] = False  # no timer decides anything from here: the scan and the cleanup run to the end (T-4820)
            signal.setitimer(signal.ITIMER_PROF, 0)
        # never an empty verdict for a script not fully read: the whole text by plain regexes, and the scripts it runs
        # (bash X, source X, ./X: up to 8 files, one level of those too)
        todo, seen = ([] if finished else list(_TEXTS)), set()
        reached = [(lex, p)] + [(sp, sp) for sp, _ in _TEXTS]  # every file the cut-off reads touched: (as named, resolved)
        while todo and len(seen) < 8:
            sp, st = todo.pop(0)
            if sp in seen:
                continue
            seen.add(sp)
            found += [("system", f"{sp} line {n}: {line[:80]} ({why}; the script was too long to parse in time)")
                      for n, line, why in _literal_scan(st, ctx) + _unfinished_removal(st)]
            targets = _run_targets(st)
            for target in targets[8:]:  # not read, but a file it names must still be a system file to be exempt
                lex = os.path.normpath(os.path.join(cwd, os.path.expanduser(target)))
                if os.path.isfile(os.path.realpath(lex)):
                    reached.append((lex, os.path.realpath(lex)))
                _DEPS.append((lex, _file_digest(lex)))  # a file created or changed later invalidates a cached allow
            for target in targets[:8]:  # ponytail: 8 files; a script past that, or named by a variable, is unread
                lex = os.path.normpath(os.path.join(cwd, os.path.expanduser(target)))
                tp, raw = os.path.realpath(lex), None
                if tp not in seen and os.path.isfile(tp):
                    reached.append((lex, tp))
                    try:
                        with open(tp, "rb") as f:
                            raw = f.read(_SCRIPT_LIMIT + 1)
                    except OSError:
                        pass
                _DEPS.append((lex, hashlib.sha256(raw).hexdigest() if raw is not None else ""))  # absent: stays absent
                if raw is not None and len(raw) <= _SCRIPT_LIMIT:
                    todo.append((tp, raw.decode("utf-8", "replace")))
        known = {b for _a, b in reached}
        deeper = any(os.path.isfile(tp) and tp not in known  # a file the unread ones name that no check above looked at
                     for _sp, st in todo for tp in (os.path.realpath(os.path.join(cwd, os.path.expanduser(t)))
                                                    for t in _run_targets(st)))
        if too_long and not deeper and all(_system_script(a, b) for a, b in reached):
            # a system file (every file it touched: root's, unwritable here, in unwritable directories) is no more
            # dangerous unread than a binary, which is allowed unread; what the scans above found stands
            system = list(dict.fromkeys(reached))
            unread = (f"system: read past its {_script_budget():g} s, then {_script_hard():g} s budget; a file root "
                      "owns, in directories you cannot write: allowed unread, like a binary")
        elif too_long:  # the literal scan above cannot judge what it never reached: not an allow, whatever the text holds
            unread = f"refused: read past its {_script_budget():g} s, then {_script_hard():g} s budget"
            found.append(("system", f"{p}: the script was too long to read in time ({_script_budget():g} s, then "
                                    f"{_script_hard():g} s), so it, or a script it runs, was not read to the end: split it "
                                    "into smaller scripts or run its steps as separate commands"))
    finally:
        _IN_SCRIPT.pop()
        if old is not None:
            signal.setitimer(signal.ITIMER_PROF, 0)
            signal.signal(signal.SIGPROF, old)
    if nested:
        return found  # the owner's read decides what is cached
    if unread:
        _UNREAD.append((p, unread))
    if key:
        _cache_store(path, key, {"p": p, "f": found, "u": unread, "s": [list(x) for x in system], "at": time.time(),
                                 "n": [[dp, dd] for dp, dd in dict.fromkeys(_DEPS)]})
    return found


def _script_target(name, argv0, args):
    """(kind, path): the script file a command runs: `sh` for source, `.` and a shell's script word, `py` for python,
    `exec` for a path-named command. None where the code comes from -c, a module, stdin or -n (syntax check only), or
    the command isn't one that runs a script file. Options are read as the program reads them: -n counts only
    before the script word, and a value-taking option (-o, -W, -X) takes its value."""
    if name in ("source", "."):
        return ("sh", next((a for a in args if a != "--"), None))
    py = name.startswith("python")
    if not (py or _SH_NAME.match(name)):
        return ("exec", argv0) if "/" in argv0 else (None, None)
    i = 0
    while i < len(args):
        a, i = args[i], i + 1
        if a == "--":
            return (("py" if py else "sh"), args[i] if i < len(args) else None)
        if a == "-" or not a.startswith(("-", "+")):
            return (None, None) if a == "-" else (("py" if py else "sh"), a)
        if a.startswith("--"):
            if a in ("--rcfile", "--init-file", "--check-hash-based-pycs"):
                i += 1
            continue
        for j, ch in enumerate(a[1:], 1):
            if py and ch in "cm" or not py and ch in "cs":
                return (None, None)  # code from -c, a module, or stdin
            if not py and ch == "n" and a[0] == "-":
                return (None, None)  # -n: reads the script for syntax only
            if py and ch in "WXQ" or not py and ch in "oO" and j == len(a) - 1:
                if not (py and j < len(a) - 1):  # -Wonce: the value is attached; -W once: the next word
                    i += 1
                if not py and args[i - 1:i] == ["noexec"]:
                    return (None, None)
                break
    return (None, None)


def _env_channels(argv, name):
    """[(why, code or None)] for variables a command sets that make git (or any program) run a command, or that point
    git at config, templates or programs from elsewhere (None: refused, it can't be read here)."""
    out = []
    for var, val in _assignments(argv):
        if var == "ENV" and not (name == "sh" or name == "bash" and "--posix" in argv):
            continue  # ENV=production make: only an sh (or bash --posix) reads a startup file from it
        if var in _CODE_VARS and val.strip():  # T-0275
            hit = _CODE_VARS[var](val)
            if hit is not False:
                out.append((f"{var}={_clip(val)} loads code into the next program", hit))
        elif var in _GIT_EXEC_ENV and val.strip():
            out.append((f"{var} set to a command", val))
        elif var in _GIT_CONFIG_SOURCES and val not in ("", "/dev/null"):
            out.append((f"{var} points git at config, templates or programs from elsewhere", None))
        elif var in ("HOME", "XDG_CONFIG_HOME") and name == "git":
            out.append((f"git with {var} moved (its config is read from there)", None))
        elif _RUSTFLAGS.match(var) and any(w.startswith("@") for w in re.split(r"[\s\x1f]+", val)):  # T-3607 review
            out.append((f"{var} gives rustc @FILE arguments the guard can't read here", None))
        elif re.fullmatch(r"(?i)GIT_CONFIG_(KEY_\d+|VALUE_\d+|PARAMETERS|COUNT)", var) and any(
                _GIT_EXEC_KEYS.match(k) or k.lower().startswith(("alias.", "include")) or k.lower() == "protocol.ext.allow"
                for k in (x.strip("'\" ").partition("=")[0] for x in re.split(r"'\s+'|\s+", val))):  # review: key=value
            out.append(("git config that runs a command, set through the environment", None))
    return out


def _env_files(argv):
    """Files git writes because of a variable (GIT_TRACE=/path, GIT_INDEX_FILE=/path); 1, 2, true… mean stderr."""
    return [val for var, val in _assignments(argv) if _GIT_FILE_ENV.match(var)
            and not re.fullmatch(r"\d*|true|false|yes|no|on|off", val.lower())]


_RUSTFLAGS = lazy(r"^(?:CARGO_\w*)?RUSTFLAGS$")  # RUSTFLAGS, CARGO_BUILD_, CARGO_ENCODED_ (split on \x1f), a profile's
# T-3607: settings that change what a cargo build compiles, or with what: a wrapper, flags, another rustc, linker,
# toolchain or PATH, cargo's home or a cargo config key. Not the folders it writes (judged as writes), nor what only
# changes how cargo prints, fetches, caches or optimizes (TERM_, NET_, INCREMENTAL, PROFILE_), but a profile's rustflags
# or panic strategy (abort turns a panic into a hook error, which lets the call through)
_CARGO_SETTINGS = lazy(r"^(?:RUSTC\w*|RUSTFLAGS|RUSTDOC\w*|RUSTUP_\w+|PATH|CARGO_(?!(?:BUILD_)?TARGET_DIR$|INSTALL_ROOT$|"
                       r"TERM_|NET_|INCREMENTAL$|PROFILE_(?!\w*_(?:RUSTFLAGS|PANIC)$))\w+)$")
# cargo --config KEY=VALUE (a dotted TOML key); anything else, or a value ending .toml, is a file it loads
_CARGO_INLINE = lazy(r"""^\s*(?:[\w-]+|"[^"]*"|'[^']*')(?:\s*\.\s*(?:[\w-]+|"[^"]*"|'[^']*'))*\s*=(?!.*\.toml\s*$)""",
                     re.S)
_CARGO_CONFIG = lazy(r"(?:^|/)\.cargo/config(?:\.toml)?$")


def _strings(text):
    """T-3607: the quoted strings in a text, each kind read on its own (a shell's '…' around TOML's "…" still yields
    the inner one)."""
    return re.findall(r'"([^"]*)"', text) + re.findall(r"'([^']*)'", text)


def _names_main(text, bases, ctx):
    """T-3607 review: whether a string in a cargo config's text, read as a path (~ and $HOME expanded, from each base
    folder, normalized, and where it really is), lands in the main checkout; a lane beside it (foreman.lanes) doesn't."""
    homes = _homes(ctx)
    return any(_under(q, fh) for s in _strings(text) for p in {_resolve(_expand(s.strip(), ctx), b) for b in bases}
               for q in (p, os.path.realpath(p)) for fh in homes)


def _cargo_config_hit(target, text, cwds, ctx):
    """T-3607 review: a .cargo/config anywhere (a lane's, the lanes folder above them) with a path into the main
    checkout points every build under it there (target-dir, rustc-wrapper); rustc @FILE flags in it can't be read.
    Paths are read from the config's folder and the one above .cargo (where cargo resolves most). ponytail: only text
    the guard reads (a tool's content, the command line); a file copied in, or what a lane's build.rs writes, isn't,
    so this can't close it."""
    if not _CARGO_CONFIG.search(target):
        return []
    found = [("system", f"{target}: a cargo config with rustc @FILE arguments the guard can't read here")] if any(
        s.lstrip().startswith("@") for s in _strings(text)) else []
    dirs = list(cwds) if _unresolvable(target) else [os.path.dirname(_resolve(_expand(target, ctx), c)) for c in cwds]
    if _names_main(text, dirs + [os.path.dirname(d) for d in dirs], ctx):
        found.append(("core", f"{target} (a cargo config naming Sampo's main checkout: builds under it write or run there)"))
    return found


def _rustup_writes(args):
    """T-3607 review: what rustup writes into the toolchain it runs: a linked toolchain (from any folder), and a default
    or override given as a path."""
    words, rh = [a for a in args if not a.startswith("-")], os.environ.get("RUSTUP_HOME") or "~/.rustup"
    if words[:2] == ["toolchain", "link"] and len(words) > 2:
        return [os.path.join(rh, "toolchains", words[2])]
    head = 1 if words[:1] == ["default"] else 2 if words[:2] == ["override", "set"] else 0
    return [os.path.join(rh, "settings.toml")] if head and any("/" in w for w in words[head:]) else []


def _rustc_outs(args):
    """T-3607: what rustc writes: -o FILE, --out-dir DIR, each --emit KIND=PATH, -C incremental=DIR."""
    return _opt_values(args, "-o", "--out-dir") + [
        v.partition("=")[2] for e in _opt_values(args, "--emit") for v in e.split(",") if "=" in v] + [
        v[12:] for v in _opt_values(args, "-C", "--codegen") if v.startswith("incremental=")]


def _build_outs(name, args, argv):
    """T-3607: where a cargo or rustc call writes: its output folders (trees: a profile or target name picks the files
    in them), rustc's own outputs (after cargo's --), and the folder or rustc flags a variable sets, on any command
    (CARGO_TARGET_DIR=…, export RUSTFLAGS='-o …')."""
    out = []
    for k, v in _assignments(argv):
        if k in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "CARGO_BUILD_ARTIFACT_DIR", "CARGO_BUILD_BUILD_DIR",
                 "CARGO_INSTALL_ROOT"):
            out.append(v)
        elif _RUSTFLAGS.match(k):
            out += _rustc_outs(re.split(r"[\s\x1f]+", v))
    if name == "cargo":
        n = args.index("--") if "--" in args else len(args)
        out += _opt_values(args[:n], "--target-dir", "--artifact-dir", "--out-dir", "--root") + _rustc_outs(args[n + 1:])
        # T-3607 review: every string in an inline --config (build.target-dir, install.root, rustflags' -o FILE)
        out += [s for v in _opt_values(args[:n], "--config") if _CARGO_INLINE.match(v) for s in _strings(v)
                if not s.startswith(("-", "@"))]
    elif name == "rustc":
        out += _rustc_outs(args)
    elif name == "rustup":
        out += _rustup_writes(args)
    return out


def _build_cats(path, ctx):
    """T-3607: a build output folder's categories: what it is under and what lies under it; never confine or brief, as
    a build's own target/ is written unasked (a lane builds into the shared cargo target folder)."""
    cats = [k for k in dict.fromkeys(classify_write(path, ctx) + classify_tree(path, ctx)) if k not in ("confine", "brief")]
    if "state-direct" in cats and _cargo_assigned(path, ctx):  # T-4742: the one folder of Sampo's state a build owns
        cats = [k for k in cats if k != "state-direct"]  # (every other category the path has stays)
    return cats


def _cargo_assigned(path, ctx):
    """T-4742: whether path is the cargo target folder Sampo assigned this project (ctx.cargo_target: the folder, or a
    function giving it) or inside it: as written with no '..' in it (the lexical a/link/../x is not where the kernel
    goes), and as it really resolves (a link out of the folder is no longer inside). Its siblings, the cache folder
    above it and the rest of state stay refused. The caller drops only state-direct, and only for a cargo, rustc or
    rustup call's own outputs."""
    d = getattr(ctx, "cargo_target", None)
    d = d() if callable(d) else d
    if not d or not isinstance(path, str) or not os.path.isabs(path) or ".." in path.split("/"):
        return False
    return _under(path, d) and _under(os.path.realpath(path), os.path.realpath(d))


_INSTALLER_SCRIPTS = ("fastbuild.py", "fmfastbuild.py")
_INSTALLER_MODULES = ("sampo.fastbuild", "sampo.fmfastbuild", "fmfastbuild", "fastbuild")
_ENV_BUILTINS = ("export", "declare", "typeset", "readonly", "unset", "set", "local")


def _installer_doctor(rest):
    """T-3608: sampo doctor's flags that build and install the native binary: --repair and --fix, and the shortest
    abbreviations argparse would take for them. doctor also has --restore-state and --full, so argparse refuses --r, --re
    and --f as ambiguous; the guard reads each of them as the installer anyway (nothing runs, and a prefix of one
    flag the guard blocks is not worth deciding)."""
    verb, after = _fm_subcommand(rest)
    flags = [a.partition("=")[0] for a in after if a.startswith("--")]
    return verb == "doctor" and any(
        len(f) >= 3 and ("--repair".startswith(f) or "--fix".startswith(f)) for f in flags)


def _installer_argv(argv, stdin=""):
    """T-3608: argv (wrappers dropped) builds and installs plugin/native's sampo-fast: sampo doctor --repair|--fix, a
    direct run of fastbuild.py (by path, python script, -m or -c) or of install.sh (not bash -n), also a shell that
    reads install.sh from stdin (bash < install.sh): `stdin` is the file a < redirect gives it."""
    if not argv:
        return False
    name, args = os.path.basename(argv[0]), argv[1:]
    if _fm_argv(argv):
        return _installer_doctor(args if name in FM_PROGRAMS else args[1:])
    if name in _INSTALLER_SCRIPTS or name == "install.sh":
        return True
    py = re.match(r"^python[0-9.]*$", name)
    if py:
        for i, a in enumerate(args):
            if a == "-m" and args[i + 1:i + 2] and args[i + 1] in _INSTALLER_MODULES:
                return True
            if a == "-c" and "fastbuild" in " ".join(args[i + 1:i + 2]):
                return True
        script = next((a for a in args if not a.startswith("-")), "")
        return os.path.basename(script) in _INSTALLER_SCRIPTS
    if name in ("source", ".") or _C_SHELLS.match(name):
        opts = [a for a in args if a.startswith("-") and not a.startswith("--")]
        script = next((a for a in args if not a.startswith("-")), "")
        if any("n" in o[1:] for o in opts):
            return False
        if os.path.basename(script) == "install.sh":
            return True
        # a shell with no script (or -s: the words after are arguments) reads its commands from stdin
        return bool(stdin) and os.path.basename(stdin) == "install.sh" and name not in ("source", ".") and (
            not script or "s" in "".join(o[1:] for o in opts))
    return False


def _installer_scan(cmds, depth=0):
    """T-3608: (installs, sets) over the commands and the text a `sh -c` or eval runs: whether one builds the binary
    and whether one sets the environment it runs in (an assignment before a command, env, export…)."""
    installs = sets = False
    for c in cmds:
        argv, _ = _strip_wrappers(c.argv)
        pre = c.argv[:len(c.argv) - len(argv)]
        installs = installs or _installer_argv(argv, c.stdin or "")
        sets = sets or bool(_assignments(c.argv)) or any(os.path.basename(w) == "env" for w in pre) or bool(
            argv) and os.path.basename(argv[0]) in _ENV_BUILTINS
        text = None
        if argv and depth < 3 and _C_SHELLS.match(os.path.basename(argv[0])):
            i = next((j for j, a in enumerate(argv[1:], 1) if re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", a)), 0)
            text = argv[i + 1] if i and i + 1 < len(argv) else None
        elif argv and depth < 3 and os.path.basename(argv[0]) == "eval":
            text = " ".join(argv[1:])
        if text:
            inner = _installer_scan(_split(_tokens(_lines(text))), depth + 1)
            installs, sets = installs or inner[0], sets or inner[1]
        if argv and depth < 3 and os.path.basename(argv[0]) == "find":  # find … -exec CMD … ; (also -execdir, -ok, -okdir)
            ran, i = [], 1
            while i < len(argv):
                if argv[i] in ("-exec", "-execdir", "-ok", "-okdir"):
                    j = next((k for k in range(i + 1, len(argv)) if argv[k] in (";", "+", "\\;")), len(argv))
                    ran.append(Cmd(argv[i + 1:j], [], False))
                    i = j
                i += 1
            if ran:
                inner = _installer_scan(ran, depth + 1)
                installs, sets = installs or inner[0], sets or inner[1]
    return installs, sets


# T-3608 round 4: the general rule behind the specific ones. A list of start forms keeps losing (bash -o pipefail <
# install.sh, bash /dev/stdin < install.sh, exec 0<install.sh; bash …), so the line is read as a whole: it names an
# installer file (a word or the tail of a path) and something on it runs code that may take that file as its program.
_INSTALLER_NAMED = lazy(r"(?<![\w.-])(?:install\.sh|(?:fm)?fastbuild\.py|(?:sampo\.)?(?:fm)?fastbuild|build_fast)(?![\w.-])")
_INTERPRETERS = lazy(r"^(?:python[0-9.]*|pypy[0-9.]*|perl|ruby|node|nodejs|deno|bun|php)$")
_CHECKER_MODULES = {"py_compile", "compileall", "pyflakes", "flake8", "mypy", "ruff", "black", "pycodestyle", "pylint",
                    "ast", "tokenize"}  # python -m …: they read a source file, they don't run it
_URLS = lazy(r"(?<![\w+.-])[A-Za-z][\w+.-]*://\S*")  # anchored at a word's start: linear on a long word
_COMPUTED_WORD = lazy(r"[$`]|<\(|^/dev/fd/|^/proc/self/fd/")  # a word the guard can't read as a name


def _names_installer(text):
    """Whether text names one of the installer files as a word or a path tail. A URL's tail isn't a file of ours: a
    downloaded install.sh piped to a shell is the pipe-shell rule's (T-0589), not this one's."""
    if not _INSTALLER_NAMED.search(text):  # the common line, one linear scan
        return False
    return bool(_INSTALLER_NAMED.search(_URLS.sub(" ", text)))


def _kind(name):
    return ("sh" if re.fullmatch(r"(ba|z|da|k|fi|c|tc|a|mk|ya|po|lk)?sh|busybox", name) else
            "py" if re.fullmatch(r"python[0-9.]*|pypy[0-9.]*", name) else "js" if name in ("node", "nodejs", "deno", "bun")
            else "pl" if name == "perl" else "rb" if name == "ruby" else "php" if name == "php" else None)


def _program_words(name, args):
    """(the words of a shell's or interpreter's own command line that are its program, its inline code or None): the
    script (the first word that is neither an option nor an option's value), the module of -m and the values its other
    options take (--rcfile, -o, -r…). The words after the script or the code are its arguments, data it reads:
    `python3 plugin/bin/sampo lease take install.sh` runs sampo, `perl -pi -e 's/a/b/' install.sh` edits the file."""
    if name in ("source", "."):
        return args[:1], None
    kind = _kind(name)
    if kind is None:
        return list(args), None
    out, i = [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            return out + args[i:i + 1], None
        if a.startswith("--"):
            if kind == "js" and a in ("--eval", "--print") or kind == "sh" and a == "--command":
                return out, args[i] if i < len(args) else None
            if "=" in a:
                out.append(a.split("=", 1)[1])
            elif a in _LONG_VALUE:
                out += args[i:i + 1]
                i += 1
            continue
        if a.startswith(("-", "+")) and len(a) > 1:
            for j, f in enumerate(a[1:]):
                if f in _INLINE[kind] or f in _OPT_VALUE[kind]:
                    val = a[j + 2:] or (args[i] if i < len(args) else "")
                    i += not a[j + 2:]
                    if f in _INLINE[kind]:  # the code or the module: what follows is its arguments
                        return (out + [val], None) if kind == "py" and f == "m" else (out, val)
                    out.append(val)
                    break
            continue
        return out + [a], None  # the script
    return out, None


# inline code that runs something: a call that starts a program or reads and runs a file, a backtick, an import of the
# build module. Code that only holds the name (a list of file names, a path it reads) is data, not a start.
_CODE_STARTS = lazy(r"`|%x[({\[]|\bqx\b|\b(?:import|from|require|use|load)\b[^;\n]{0,80}(?<![\w.-])(?:sampo\.)?(?:fm)?fastbuild(?![\w.-])")


def _code_runs(code):
    return bool(_runs_it_installer(code) or _CODE_STARTS.search(code))


def _starts_installer(body, interp=""):
    """A heredoc body for an interpreter is an installer start by the rule of its -c text (round 6): it names the
    file and is code that runs something (_code_runs), however long the string that holds the name. One narrowing: a
    backtick or %x/qx starts a program only in a language where it does (perl, ruby, php), not in prose or in python
    and node code, where a backtick is text or a template."""
    if not _names_installer(body):
        return False
    if _runs_it_installer(body) or any(m.group(0)[:1].isalpha() for m in _CODE_STARTS.finditer(body)):
        return True
    return bool(re.match(r"(?:perl|ruby|php)", interp) and _CODE_STARTS.search(body))


def _runs_named(cmds, outer=False, depth=0, bodies=()):
    """T-3608 round 4: whether a command (the text of a -c, an eval and a find -exec read as commands too) runs code
    that may take an installer file as its program. A shell or interpreter hits when a word of its program (the
    script, an option's value, inline code: _program_words) names the file, when that word is one it can't read (a
    variable, <( )) and a word of the line names it, or when it reads its program from stdin (no script, -s, -,
    /dev/stdin) and what feeds that stdin names the file: its < redirect, its here-string, the command before the
    pipe, a <( ) (outer: the stdin of the -c shell around it). Plain reads and checkers (cat, grep, git diff, bash -n,
    python -m py_compile) hit nothing, nor does a word the program only takes as data (sampo lease take install.sh).
    An interpreter that reads its code from a heredoc (round 5; bodies: the line's heredoc bodies in order) hits when
    the body starts the build (_starts_installer); a shell's heredoc body is read as commands
    by check_bash itself.

    The limit of the name test (_names_installer) is lexical: it reads words, so a name the text builds (ins''tall.sh,
    install.s[h], $(echo install).sh, a glob, a computed word the line never spells out) or a path that reaches the
    file through a symlink made on an earlier line is not seen. Its other known limit (T-4827) is the call shape of
    inline code: a program started through a library or form the call words don't name (pexpect.run(…), the sh
    package's sh.bash(…), perl's paren-less pipe-open open P, "bash install.sh|", a call word bound to a plain name
    by an import that doesn't spell it (from os import *; system(…) is seen, but from os import *; f = system or
    o = __builtins__; … isn't), a
    paren-less call whose argument runs on with more words, and Tcl's exec) isn't seen by the call shapes. While the
    bare-word rule stands (_RUNS_IT_WORDS, until the user's core yes lets plain words through) the ones that spell a
    call word unquoted (system, exec) are still refused by it; pexpect, sh.bash and perl's open aren't. That is bounded, not a
    hole in what the build trusts:
    the build is hermetic (a scrubbed environment, the account's own cargo, rustc and a root-owned linker, a
    hash-checked private registry copy, a fresh target) and it builds only the clean committed tree, then installs the
    binary after a probe against the Python hook; a start the guard misses runs that same build."""
    named = _names_installer
    loose = []  # one word of the line, a path or name with no space in it, names a file: not prose that mentions one

    def names_in(x):
        return any(named(w) for w in x.argv) or bool(x.stdin and named(x.stdin))

    def line_names():
        if not loose:
            loose.append(any(named(w) for x in cmds for w in x.argv if not re.search(r"\s", w)) or any(
                x.stdin and named(x.stdin) for x in cmds))
        return loose[0]

    assigned = {}  # NAME -> the values the line gives it (R=…/retry.sh; bash $R …: the script is the value, not unknown)
    for x in cmds:
        for key, val in _assignments(x.argv):
            assigned.setdefault(key, []).append(val)

    def values(a):  # the words a program word may be: its own, or those of the variable it is
        m = re.fullmatch(r"\$(\w+)|\$\{(\w+)\}", a)
        return assigned.get((m.group(1) or m.group(2)) if m else "") or [a]

    heredoc, n = {}, 0  # the command's own heredoc bodies: the Nth `<<` of the line is the Nth body
    for k, c in enumerate(cmds):
        for t, _ in c.feeds:
            if t == "<<":
                heredoc.setdefault(k, []).append(bodies[n] if n < len(bodies) else "\n".join(bodies))
                n += 1

    exec_fed = False  # exec 0<install.sh: every shell after it reads the file
    for k, c in enumerate(cmds):
        argv, via_xargs = _strip_wrappers(c.argv)
        exec_fed = exec_fed or c.argv[:1] == ["exec"] and len(c.argv) == 1 and bool(c.stdin and named(c.stdin))
        if not argv or depth > 3:
            continue
        fed = outer or exec_fed or bool(c.stdin and named(c.stdin)) or any(w and named(w) for t, w in c.feeds if t == "<<<")
        if not fed and c.procsub:
            fed = any(names_in(x) for x in cmds[k + 1:] if x.depth > c.depth)
        j = k - 1 if c.piped else -1
        while not fed and j >= 0:  # every command of the pipeline before it
            fed = names_in(cmds[j])
            j = j - 1 if cmds[j].piped else -1
        # { bash; } < install.sh, (bash) < install.sh, done < install.sh: the redirect sits on the closing word
        fed = fed or any(x.stdin and named(x.stdin) for x in cmds[k + 1:] if x.argv in ([], ["}"], ["done"]))
        i = 0
        if _skips_to_runner(os.path.basename(argv[0])):  # T-4859: _WRAPPER_SPEC; the words after it run: its first runner word on is the command
            i = next((j for j, a in enumerate(argv) if j and _is_runner(os.path.basename(a))), len(argv))
            for a in argv[1:]:
                if re.search(r"\s", a) and _runs_named(_split(_tokens(_lines(a))), fed, depth + 1):
                    return True
        if i >= len(argv):
            continue
        name, args = os.path.basename(argv[i]), argv[i + 1:]
        words = " ".join(args)
        prog, code = _program_words(name, args)
        prog = [v for a in prog for v in values(a)]
        computed = c.procsub or any(_COMPUTED_WORD.search(a) for a in prog if not a.startswith("-"))  # no name to read
        reads_stdin = _reads_stdin_code(name, args, via_xargs) or via_xargs and not prog and code is None
        if name == "eval":
            if named(words) or any(_COMPUTED_WORD.search(a) for a in args) and line_names():
                return True
        elif name == "exec":
            if _runs_named([Cmd(args, [], False)], fed, depth + 1):
                return True
        elif name in ("source", ".") or _RUNS_STDIN.fullmatch(name) or _INTERPRETERS.match(name):
            opts = [a for a in args if re.match(r"^-[A-Za-z]", a)]
            if _RUNS_STDIN.fullmatch(name) and any(o[1:2] != "-" and "n" in o[1:] for o in opts if not o.startswith("--")):
                continue  # bash -n reads the script, runs none of it
            if _INTERPRETERS.match(name) and "-m" in args and args[args.index("-m") + 1:][:1] and \
                    args[args.index("-m") + 1].split(".")[0] in _CHECKER_MODULES:
                continue
            ci = next((k for k, a in enumerate(args) if _C_SHELLS.match(name) and re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", a)), None)
            if ci is not None and ci + 1 < len(args):  # sh -c 'text': the text is commands of their own, on the same stdin
                text = args[ci + 1]
                if re.fullmatch(r"\s*(?:\$\(.*\)|`.*`|\$\w+|\$\{\w+\})\s*", text, re.S) and (named(text) or line_names()):
                    return True  # -c "$(cat install.sh)", -c "$f"
                if _runs_named(_split(_tokens(_lines(text))), fed, depth + 1):
                    return True
                continue
            if any(named(a) for a in prog) or code and named(code) and _code_runs(code) or computed and line_names() or \
                    reads_stdin and fed:
                return True
            if reads_stdin and _INTERPRETERS.match(name) and any(_starts_installer(b, name) for b in heredoc.get(k, ())):
                return True  # python3 - <<'EOF' … subprocess.run(['bash', 'install.sh']) … EOF
    for c in cmds:  # find … -exec CMD … ;
        argv, _ = _strip_wrappers(c.argv)
        if argv and depth < 3 and os.path.basename(argv[0]) == "find":
            ran, k = [], 1
            while k < len(argv):
                if argv[k] in ("-exec", "-execdir", "-ok", "-okdir"):
                    j = next((n for n in range(k + 1, len(argv)) if argv[n] in (";", "+", "\\;")), len(argv))
                    ran.append(Cmd(argv[k + 1:j], [], False))
                    k = j
                k += 1
            if ran and _runs_named(ran, False, depth + 1):
                return True
    return False


def _is_runner(name):
    return bool(name in ("source", ".", "eval", "exec") or _RUNS_STDIN.fullmatch(name) or _INTERPRETERS.match(name))


def _installer_lines(cmds, raw=""):
    """T-3608: a line that builds and installs the binary hooks/fast runs (sampo doctor --repair|--fix, fastbuild.py,
    install.sh) is the user's to grant (core): the guard's own fast path comes out of it. A line that also sets the
    environment (VAR=… before it, env, export…) is refused outright (system): a variable the guard can't weigh by name
    (PATH, HOME, RUSTUP_HOME, TMPDIR, CARGO_HOME…) chooses what gets built. The specific forms above give the exact
    reason; the general rule after them (an installer file named on a line that runs code) catches the forms not listed."""
    installs, sets = _installer_scan(cmds)
    if not installs and raw and _names_installer(raw):
        installs = _runs_named(cmds, bodies=["\n".join(b) for b in _heredoc_split(raw)[1]] if "<<" in raw else ())
    if not installs:
        return []
    if sets:
        return [("system", "T-3608: a build of the native guard binary (sampo doctor --repair|--fix, fastbuild.py, "
                           "install.sh) on a line that sets the environment it runs in: run it with the account's own "
                           "environment, on a line of its own")]
    return [("core", _INSTALLER_CORE)]


def _cargo_settings(args, cmds, pre):
    """T-3607: the build settings a cargo call may run under: a variable any command on the line sets or exports
    (export RUSTC_WRAPPER=…; cargo …), --config, and a toolchain given as a path (cargo +/x, rustup run /x cargo)."""
    n = args.index("--") if "--" in args else len(args)
    tc = [a[1:] for a in args[:1] if a.startswith("+")]
    for i, w in enumerate(pre):
        if os.path.basename(w) == "rustup" and pre[i + 1:i + 2] == ["run"]:
            tc += [x for x in pre[i + 2:] if not x.startswith("-")][:1]
    return sorted({k for x in cmds for k, _ in _assignments(x.argv) if _CARGO_SETTINGS.match(k)}) + [
        "--config"] * any(a == "--config" or a.startswith("--config=") for a in args[:n]) + [
        f"toolchain {t}" for t in tc if "/" in t]


def _builds_native(args, pre, cwds, lost, known, ctx):
    """T-3607: whether a cargo call may build the main checkout's plugin/native: the folders it may run in (the cwd,
    env -C, cargo -C; one the guard lost may be anywhere) and the --manifest-path or install --path it names."""
    n = args.index("--") if "--" in args else len(args)
    words = [_with_vars(w, known) for w in _opt_values(pre, "-C", "--chdir") + _opt_values(
        args[:n], "-C", "--manifest-path", "--path")]
    if lost or any(_unresolvable(w) for w in words):
        return True
    bases = list(cwds) + [_resolve(_expand(w, ctx), b) for w in words for b in cwds]
    natives = [os.path.join(fh, "plugin", "native") for fh in _homes(ctx)]
    return any(_under(q, r) for p in bases + [_resolve(_expand(w, ctx), b) for w in words for b in bases]
               for q in (p, os.path.realpath(p)) for r in natives)


def _git_config_code(var, value):
    """(why, code or None) for a config key whose value git will run, load or alias; None when it is harmless."""
    key = var.lower()
    if key.startswith("alias."):  # an alias to a git subcommand can't be followed into the commands that use it later
        return (f"git alias {var[6:]}", value[1:]) if value.startswith("!") else (f"a git alias ({var})", None)
    if key.startswith("include") or key in ("core.hookspath", "protocol.ext.allow", "init.templatedir"):
        return (f"git config {var} (config, hooks or transports from elsewhere)", None)
    if _GIT_EXEC_KEYS.match(var) and value.strip():
        return (f"git config {var}", value.lstrip("!"))
    return None


def _git_code(args):
    """[(why, shell code or None)] for the commands a git invocation will run on its own (T-0269/T-0270): `-c` keys
    whose value is a command, a `-c alias.NAME=…` it then calls, `git config` setting such a key, subcommand options
    that take a command (rebase -x, bisect run, submodule foreach, …). None: refused outright, it can't be read here
    (--exec-path, --config-env, --template, config included from a file)."""
    out = []
    i, aliases = 0, {}
    while i < len(args) and args[i].startswith("-"):
        opt, eq, val = args[i].partition("=")
        if opt == "-c" and i + 1 < len(args):
            key, _, value = args[i + 1].partition("=")
            if key.lower().startswith("alias."):
                aliases[key[6:].lower()] = value
            else:
                hit = _git_config_code(key, value)
                out += [hit] if hit else []
        elif opt == "--exec-path" and eq:
            out.append(("git --exec-path=DIR (its subcommands run from there)", None))
        elif opt == "--config-env":
            key = (val if eq else (args[i + 1] if i + 1 < len(args) else "")).partition("=")[0]
            if _GIT_EXEC_KEYS.match(key) or key.lower().startswith(("alias.", "include")):
                out.append((f"git --config-env {key}", None))
        i += 2 if args[i] in _GIT_VALUED else 1
    sub, tail = (args[i].lower() if i < len(args) else ""), args[i + 1:]
    rest = " ".join(shlex.quote(x) for x in tail)
    if sub in aliases:  # the expansion keeps the global options (-C DIR …) the alias ran under
        value, opts = aliases[sub], " ".join(shlex.quote(x) for x in args[:i])
        out.append((f"git alias {sub}", value[1:] + " " + rest if value.startswith("!") else f"git {opts} {value} {rest}"))
    longs = [f for f in _GIT_EXEC_FLAGS.get(sub, ()) if f.startswith("--")]  # git takes --exe for --exec: expand
    tail = [next((f + a[len(a.partition("=")[0]):] for f in longs if len(a.partition("=")[0]) >= 5
                  and f.startswith(a.partition("=")[0])), a) if a.startswith("--") else a for a in tail]
    if sub == "clone":  # clone -c/--config writes the key into the new repo, and git uses it during the clone
        for kv in _opt_values(tail, "-c", "--config"):
            hit = _git_config_code(*kv.partition("=")[::2])
            out += [hit] if hit else []
    for flag in _GIT_EXEC_FLAGS.get(sub, ()):
        if flag in ("-O", "--open-files-in-pager"):  # an optional value: attached only (-Ocmd, --open-files-in-pager=cmd)
            vals = [a[len(flag) + (flag.startswith("--")):] for a in tail
                    if a.startswith(flag + "=" if flag.startswith("--") else flag) and len(a) > len(flag)]
        else:
            vals = _opt_values(tail, flag)
        out += [(f"git {sub} {flag}", v) for v in vals if v.strip()]
    if sub == "bisect" and tail[:1] == ["run"] and tail[1:]:
        out.append(("git bisect run", " ".join(shlex.quote(x) for x in tail[1:])))
    if sub == "submodule" and "foreach" in tail:
        words = [x for x in tail[tail.index("foreach") + 1:] if x not in ("--recursive", "--quiet", "-q")]
        out += [("git submodule foreach", " ".join(words))] if words else []
    if sub in ("init", "clone") and (_opt_values(tail, "--template") or any(a.startswith("--template=") for a in tail)):
        out.append((f"git {sub} --template (hooks from a template folder)", None))
    if sub == "config" and not (_GIT_CONFIG_READS & {a.partition("=")[0] for a in tail}):
        pos, skip = [], False
        for a in tail:  # positionals, without the values of options that take one
            if skip:
                skip = False
            elif a in ("-f", "--file", "--blob", "--type", "--default", "--comment", "--value"):
                skip = True
            elif not a.startswith("-"):
                pos.append(a)
        if pos[:1] == ["set"]:
            pos = pos[1:]
        elif pos[:1] and pos[0] in ("get", "list", "unset", "rename-section", "remove-section", "edit"):
            pos = []
        if len(pos) >= 2:
            hit = _git_config_code(pos[0], pos[1])
            out += [hit] if hit else []
        elif pos[:1] and pos[0].lower().startswith("include"):
            out.append((f"git config {pos[0]}", None))
    return out


def _tree_targets(name, args):
    """Directories a command rewrites as a whole (checkouts, extractions, recursive copies, rsync): whatever they
    contain can change, so classify_tree checks what lies under them."""
    recursive = any(a in ("-r", "-R", "-a", "--recursive", "--archive") or re.fullmatch(r"-[a-zA-Z]*[rRa][a-zA-Z]*", a)
                    for a in args)
    if name in ("git", "tar", "gtar", "bsdtar", "unzip", "cpio", "7z", "7za", "7zz", "rsync") or \
            (name in ("cp", "install") and recursive):
        return _write_targets(name, args)
    return []


def _git_sub(args):
    """The git subcommand `git ARGS` runs, past its global options ("" for none)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_VALUED else 1
    return args[i] if i < len(args) else ""


_LANE_ROOT = lazy(r"(.+)\.lanes/[^/]+|(.+)/\.claude/worktrees/[^/]+")
_LANE_MERGE_FLAGS = {"--no-edit", "--no-commit", "--no-ff", "--ff-only", "-q", "--quiet"}


def _merge_branch(args):
    """T-3537, T-4340: the one branch of `git merge BRANCH [--no-edit …]` when its words are only that and the flags that
    change nothing about where it runs or what it writes (_LANE_MERGE_FLAGS); else None. Both lane exemptions read it."""
    pos = [a for a in args[1:] if not a.startswith("-")]
    return pos[0] if args[:1] == ["merge"] and len(pos) == 1 and set(args[1:]) - {pos[0]} <= _LANE_MERGE_FLAGS else None


def _lane_merge(args, cwds):
    """T-3537: whether `git ARGS`, run in each of cwds, merges the build branch (the one its main checkout has out) into
    a lane: each cwd a lane's own root, <repo>.lanes/<ID> or <repo>/.claude/worktrees/<name>, whose .git file links
    into <repo>/.git/worktrees/. The lane lesson's step before re-review; it rewrites only that lane's checkout."""
    pos = [_merge_branch(args)]
    if pos[0] is None or not cwds:
        return False
    for d in cwds:
        real = os.path.realpath(d)
        m = _LANE_ROOT.fullmatch(real)
        if not m:
            return False
        repo = m[1] or m[2]
        try:
            with open(os.path.join(real, ".git")) as f:
                link = f.read().strip()
            with open(os.path.join(repo, ".git", "HEAD")) as f:
                head = f.read().strip()
        except OSError:  # no .git file (the main checkout has a folder) or no HEAD: no lane
            return False
        gitdir = os.path.realpath(os.path.join(real, link.removeprefix("gitdir: ")))
        if not link.startswith("gitdir: ") or os.path.dirname(gitdir) != os.path.realpath(
                os.path.join(repo, ".git", "worktrees")) or head != f"ref: refs/heads/{pos[0]}":
            return False
    return True


def _briefed_lane_merge(args, cwds, ctx):
    """T-4340: whether `git merge X` runs in a lane's own root that the hook found its one task active in (ctx.briefed_lanes:
    a worktree's top, checked by the hook): a sibling branch merged into the lane's checkout by the session that has no
    task of its own. Only the merge, only each cwd exactly that root (no -C, no subfolder), the same flags as _lane_merge."""
    lanes = {os.path.realpath(d) for d in getattr(ctx, "briefed_lanes", ())}
    return (_merge_branch(args) is not None and bool(cwds) and all(os.path.realpath(d) in lanes for d in cwds))


def write_targets(cmd):
    """T-0971 review: the paths a Bash command writes, as the guard reads them (redirects and the writers it knows),
    unresolved; [] when it can't be parsed."""
    out = []
    try:
        for x in _split(_tokens(cmd)):
            if x.argv:
                stripped, _ = _strip_wrappers(x.argv)
                out += list(x.redirs) + (_write_targets(os.path.basename(stripped[0]), stripped[1:]) if stripped else [])
    except Exception:
        return []
    return [t for t in out if isinstance(t, str) and t and t != "/dev/null"]


def _new_branch_only(sub, rest):
    """T-1598: whether git checkout/switch only makes a branch at HEAD (checkout -b NAME, switch -c NAME): no start
    point, no pathspec, so no file changes. Quiet and tracking flags aside, anything else is read as a tree write."""
    # -B and -C reset a branch that may exist: kept as tree writes
    flags = {"checkout": ("-b",), "switch": ("-c", "--create")}[sub]
    words = [a for a in rest if a not in ("-q", "--quiet", "--track", "--no-track")]
    if len(words) == 1 and sub == "switch":
        return bool(re.fullmatch(r"--create=[^-\s][^\s]*", words[0]))
    return len(words) == 2 and words[0] in flags and not words[1].startswith("-")


def _checkout_files(base, rest):
    """The files `git checkout` restores when it names only files (after `--`, or every word an existing path and
    no commit), else [] (a branch, -b/-B/--orphan, `.`, a pathspec magic word or a word that is also a commit: the
    whole tree). T-1195."""
    if "--" in rest:
        if any(a.startswith("--p") for a in rest[:rest.index("--")]):  # --pathspec-from-file, or a prefix of it
            return []
        files = rest[rest.index("--") + 1:]
    else:
        if any(a.startswith("-") and a != "-" for a in rest):  # -b, -B, --orphan, --pathspec-from-file, -p ...
            return []
        files = rest
        if not files or any(not os.path.exists(os.path.join(base, a))
                            or _is_commit(base, a) for a in files):
            return []
    # git expands globs in a pathspec (and GIT_GLOB_PATHSPECS / GIT_ICASE_PATHSPECS change how), so a word with a
    # glob character names files the guard can't list
    if any(f.startswith(":") or f.startswith("-") or os.path.normpath(f) == "." or re.search(r"[*?\[\\]", f)
           for f in files):
        return []
    return files


def _inplace_switch(a):
    """T-1585 re-review: whether a perl or ruby switch cluster holds -i; the rest of a cluster after a switch that
    takes a value (-M, -I, -e, -r…) is that value (-MFile::Basename has no -i)."""
    if not a.startswith("-") or a.startswith("--"):
        return False
    for ch in a[1:]:
        if ch == "i":
            return True
        if ch in "0CdDeEFIlmMxrKWT":
            return False
    return False


def _under_git_c(base, p):
    """T-1487: p as git reads it under its -C folder. Joined onto "." an absolute $D read as a folder under the cwd
    (D=/x; git init $D refused as a write over the cwd); a value only known later ($D) is read both ways."""
    if base == "." or p.startswith(("/", "~")):
        return [p]
    return [os.path.join(base, p)] + ([p] if p.startswith("$") else [])


def _write_targets(name, args):
    """Paths a command writes (a directory means anything under it). Archives, clones and -t target dirs included:
    the guard can't see an archive's members, so the destination itself is what's checked."""
    pos = _positionals(args)
    if name == "tee":
        return pos
    if name in ("cp", "mv", "install", "ln", "rsync"):
        tdir = _opt_values(args, "-t", "--target-directory") if name != "rsync" else []  # rsync -t: keep times
        out = tdir + ([pos[-1]] if len(pos) >= 2 else [])
        if name == "mv" or name == "rsync" and "--remove-source-files" in args:  # T-1744: a move removes its
            out += pos if tdir else pos[:-1]  # sources, so moving a protected file out is a write where it was
        return out
    if name in ("tar", "gtar", "bsdtar"):
        return _tar_targets(args)
    if name == "unzip":
        return [] if set(args) & {"-l", "-t", "-v", "-Z", "-p", "-c"} else _opt_values(args, "-d") or ["."]
    if name == "cpio":
        extract = any("i" in a[1:] for a in args if a.startswith("-") and not a.startswith("--")) or "--extract" in args
        return (_opt_values(args, "-D", "--directory") or ["."]) if extract else []
    if name in ("7z", "7za", "7zz") and pos and pos[0] in ("x", "e"):
        return _opt_values(args, "-o") or ["."]
    if name == "git":
        i, where, trees = 0, [], []
        while i < len(args) and args[i].startswith("-"):  # global options before the subcommand
            opt, eq, val = args[i].partition("=")
            if opt in ("-C", "--git-dir", "--work-tree") and not eq:
                val = args[i + 1] if i + 1 < len(args) else ""
            if opt == "-C":
                where.append(val)
            elif opt in ("--git-dir", "--work-tree"):
                trees.append(val)
            i += 2 if args[i] in _GIT_VALUED else 1
        bases = ["."]  # T-1487: an absolute -C (/x, ~) replaces the folder before it; a $D is read both ways (review)
        for w in where:
            bases = list(dict.fromkeys(x for b in bases for x in _under_git_c(b, w)))
        if i < len(args) and args[i] == "clone":  # T-1745: its destination is read under the -C folders
            rest = _positionals(args[i + 1:])
            made = [rest[-1]] if len(rest) >= 2 else ["."]
            return [x for b in bases for d in made + _opt_values(args[i + 1:], "--separate-git-dir")
                    for x in _under_git_c(b, d)]
        sub, rest = args[i] if i < len(args) else "", args[i + 1:]
        # T-0269 review: --output=FILE writes a file from any diff-family subcommand (diff, log, show, stash show);
        # T-0270: the files and folders other subcommands write
        outs = [a.split("=", 1)[1] for a in rest if a.startswith("--output=")] + \
            [b for a, b in zip(rest, rest[1:]) if a == "--output"] + _opt_values(rest, "--separate-git-dir")
        outs += _git_writes(sub, rest)
        outs = [x for b in bases for o in outs for x in _under_git_c(b, o)]
        read_only = sub == "stash" and rest[:1] in (["list"], ["show"])  # T-0265: listing a stash writes nothing
        if sub == "apply":  # T-2661: only these flags, at least one: nothing is applied (--stat --apply would apply)
            flags = [a for a in rest if a.startswith("-") and a != "--"]
            read_only = bool(flags) and all(f in ("--check", "--stat", "--numstat", "--summary") for f in flags)
        # T-0935: reset moves HEAD and the index; only --hard/--merge/--keep (or a unique prefix of one, git takes
        # those; or --recurse-submodules) rewrite files
        if sub in ("checkout", "switch") and _new_branch_only(sub, rest):  # T-1598: a branch at HEAD, no files
            read_only = True
        if sub == "reset" and not any(a.startswith("--") and len(a) > 2 and a != "--" and (
                any(w.startswith(a) for w in ("--hard", "--merge", "--keep", "--recurse-submodules")))
                for a in rest):
            read_only = True
        if not read_only and (sub in _GIT_WORKTREE_WRITES or (  # rewrites its checkout (and repository)
                sub == "fetch" and ({"-u", "--update-head-ok"} & set(rest)))):  # fetch can move HEAD then
            if sub == "checkout" and not trees and not any(  # --icase/--glob-pathspecs change what a word matches
                    a.endswith("pathspecs") and a not in ("--literal-pathspecs", "--noglob-pathspecs")
                    for a in args[:i]) and len(bases) == 1 and (files := _checkout_files(bases[0], rest)):
                return [x for f in files for x in _under_git_c(bases[0], f)] + outs  # T-1195: named files only
            return ([x for b in bases for t in trees for x in _under_git_c(b, t)] or bases) + outs
        return outs
    if name == "sed" and any(a == "--in-place" or a.startswith("-i") for a in args):
        return pos if any(a in ("-e", "-f") for a in args) else pos[1:]
    if name[:4] in ("perl", "ruby") and not name[4:].strip("0123456789.") and \
            any(_inplace_switch(a) for a in args):  # (?:perl|ruby)[0-9.]*: perl -pi -e … FILE
        files, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in ("-e", "-E") or (a.startswith("-") and not a.startswith("--") and a.endswith(("e", "E"))):
                skip = True  # its code
            elif not a.startswith("-"):
                files.append(a)
        return files if any(a.startswith("-") and a.endswith(("e", "E")) for a in args) else files[1:]
    if name == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if name in ("truncate", "rm"):
        return pos
    if name == "touch":  # T-1680: the files it may make (check_bash judges only those that don't exist yet)
        out, i = [], 0
        while i < len(args):
            a = args[i]
            if a == "--":
                return out + args[i + 1:]
            cut = len(a) - 1 if len(a) > 1 and a[-1] in "rdt" else len(a)  # -[afhmc]*[rdt]?, a short cluster; r, d
            flags = a[:1] == "-" and not a[1:cut].strip("afhmc") and (a[1:cut], a[cut:])  # and t take the next word
            if a.startswith("--no-c") and "--no-create".startswith(a) or flags and "c" in flags[0]:
                return []  # makes nothing
            valued = len(a) > 2 and "=" not in a and any(w.startswith(a) for w in ("--reference", "--date", "--time"))
            i += 2 if valued or flags and flags[1] else 1  # GNU takes a long option's unambiguous prefix (--da)
            out += [] if a.startswith("-") else [a]  # - is its stdout
        return out
    if name == "curl":
        return [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-o", "--output")]
    if name == "wget":
        return [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-O", "--output-document")] + \
            _opt_values(args, "-P", "--directory-prefix")
    return []


def _rm_target_violation(tok, cwd, ctx, root_ok=False):
    if _unresolvable(tok):
        return f"unresolvable target {tok}"
    path = _resolve(_expand(tok, ctx), cwd)
    if path == "/":
        return "/"
    for s in list(ctx.scratch) + ["/tmp", "/var/tmp", os.environ.get("TMPDIR", "/tmp")]:
        # a scratch root that contains $HOME (odd setups, test fixtures) never exempts paths in $HOME
        if s and _strictly_under(path, s) and not (_under(path, ctx.home) and not _under(s, ctx.home)):
            return None
    if ctx.project_root and (_under if root_ok else _strictly_under)(path, ctx.project_root):
        return None
    return path


def _check_rm(name, args, via_xargs, chain, cwd, ctx, xodd=False):
    out = []
    if name == "rm":
        opts, targets, seen_dd = [], [], False
        for a in args:
            if a == "--" and not seen_dd:
                seen_dd = True
            elif a.startswith("-") and not seen_dd:
                opts.append(a)
            else:
                targets.append(a)
        recursive = any(o in ("-r", "-R") or (not o.startswith("--") and re.search(r"[rR]", o))  # T-0668: getopt takes
                        or (o.startswith("--") and len(o) > 2 and "recursive".startswith(o[2:].split("=")[0]))  # --rec
                        for o in opts)
        out_t = list(targets)
        unknown = False
        if via_xargs:  # T-1123: xargs appends what stdin holds, split on whitespace, to rm's own operands
            up = chain[-1] if chain else None  # only the command feeding xargs directly says what it gets
            uname = _name(up.argv) if up else ""
            rest = _strip_wrappers(up.argv)[0][1:] if up else []
            if xodd or not up:
                unknown = True
            elif uname == "find":
                out_t += _find_paths(rest)
            elif (_strip_wrappers(up.argv)[0][0] == "echo" and all(re.fullmatch(r"[^\s\\*?\[\]{}\"'`$%<>&;|()-][^\s\\*?\[\]{}\"'`$%<>&;|()]*", x) for x in rest)):
                out_t += rest  # plain `echo word…`: no flags, nothing xargs or echo rewrites
            else:
                unknown = True
        elif recursive and not targets:
            unknown = True
        if unknown and (recursive or via_xargs):
            out.append(("rm-outside", f"{'recursive ' if recursive else ''}rm with targets from stdin"))
        if recursive or via_xargs:
            kind = "recursive delete" if recursive else "delete"
            for t in out_t:
                v = _rm_target_violation(t, cwd, ctx, root_ok=not recursive)
                if v:
                    out.append(("rm-outside", f"{kind} of {v}"))
    elif name == "find":
        deleting = "-delete" in args or any(a in ("-exec", "-execdir", "-ok") and i + 1 < len(args)
                                            and os.path.basename(args[i + 1]) in ("rm", "shred") for i, a in enumerate(args))
        if deleting:
            for t in _find_paths(args):
                v = _rm_target_violation(t, cwd, ctx, root_ok=True)  # find deletes matches inside, not the root
                if v:
                    out.append(("rm-outside", f"find -delete under {v}"))
    return out


def _find_paths(args):
    paths = []
    for a in args:
        if a.startswith("-") or a in ("(", "!", ")"):
            break
        paths.append(a)
    return paths or ["."]


def _git(cwd, *args):
    import subprocess  # T-2023: only this helper runs a process
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _defaults(cwd):
    d = set(DEFAULT_BRANCHES)
    head = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if "/" in head:
        d.add(head.split("/", 1)[1])
    return d


def _check_git(name, args, cwd, ctx):
    if name != "git":
        return []
    i, gcwd = 0, cwd
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            gcwd = _resolve(_expand(args[i + 1], ctx), cwd)
            i += 2
        elif args[i] in _GIT_VALUED and i + 1 < len(args):
            i += 2
        else:
            i += 1
    if i >= len(args):
        return []
    sub, rest = args[i], args[i + 1:]
    out = []
    if sub == "push":
        force = any(o in ("-f", "--force", "--force-with-lease", "--force-if-includes") or o.startswith("--force-with-lease=")
                    for o in rest)
        delete = any(o in ("-d", "--delete") for o in rest)
        if "--tags" in rest:
            out.append(("publish", "git push --tags"))
        pos = [r for j, r in enumerate(rest)
               if not r.startswith("-") and not (j and rest[j - 1] in ("-o", "--push-option", "--repo"))]
        refspecs = pos[1:]
        defaults = _defaults(gcwd)
        current = _git(gcwd, "rev-parse", "--abbrev-ref", "HEAD")
        for r in refspecs:
            plus, r2 = r.startswith("+"), r.lstrip("+")
            src, dst = r2.split(":", 1) if ":" in r2 else (r2, r2)
            dst = current if dst == "HEAD" else dst.replace("refs/heads/", "")
            if ":" in r2 and src == "" and dst in defaults:
                out.append(("git-destructive", f"push deleting default branch {dst}"))
            elif delete and dst in defaults:
                out.append(("git-destructive", f"push --delete of default branch {dst}"))
            elif (force or plus) and dst in defaults:
                out.append(("git-destructive", f"force push to default branch {dst}"))
        if force and not refspecs and (current in defaults or any(o in ("--all", "--mirror") for o in rest)):
            out.append(("git-destructive", f"force push while on default branch {current}"))
    elif sub == "reset" and "--hard" in rest:
        current = _git(gcwd, "rev-parse", "--abbrev-ref", "HEAD")
        if current in _defaults(gcwd):
            out.append(("git-destructive", f"reset --hard on default branch {current}"))
    elif sub == "branch" and any(o in ("-D", "-d", "--delete") for o in rest):
        for n in _positionals(rest):
            if n in _defaults(gcwd):
                out.append(("git-destructive", f"deleting default branch {n}"))
    return out


def discards_work(cmd, cwd, depth=0):
    """T-0884: [(folder, what)] for each git command in cmd that throws away uncommitted work, what being "tree" (the
    working files) or "stash drop <ref>" / "stash clear": what the PreToolUse hook saves first. Never a refusal. A cd
    before it, a -C (~ and $VARS expanded), a shell's -c text and `git worktree remove --force <dir>` are followed;
    heredoc bodies are data. ponytail: --git-dir/--work-tree and commands python spawns aren't followed."""
    out = []
    for cm in _split(_tokens(_lines(_strip_heredocs(_join_continued(cmd))))):
        argv, _ = _strip_wrappers(cm.argv)
        name = os.path.basename(argv[0]) if argv else ""
        if name == "cd" and argv[1:2] and argv[1] != "-":
            cwd = _here(argv[1], cwd)
        elif re.fullmatch(r"(ba|z|da|k)?sh", name) and depth < 3:
            for text in _shell_c(argv[1:]):
                out += discards_work(text, cwd, depth + 1)
        if name == "git":
            out += _git_discards(argv[1:], cwd)
    return out


def _here(d, base):
    return os.path.normpath(os.path.join(base, os.path.expanduser(os.path.expandvars(d))))


def _git_discards(args, cwd):
    """[(folder, what)] when `git ARGS` run in cwd throws away uncommitted work (discards_work's test of one command)."""
    i, where = 0, cwd
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            where = _here(args[i + 1], where)
        i += 2 if args[i] in _GIT_VALUED or args[i] == "-C" else 1
    if i >= len(args):
        return []
    sub, rest = args[i], args[i + 1:]
    flags = set(rest)
    short = "".join(a[1:] for a in rest if re.fullmatch(r"-[a-zA-Z]+", a))
    pos = [a for a in rest if not a.startswith("-")]
    if sub == "stash" and pos[:1] in (["drop"], ["clear"]):
        return [(where, " ".join(["stash"] + pos[:2]))]  # not a list: a path or word fragment, not a list
    if sub == "worktree" and pos[:1] == ["remove"] and pos[1:] and (flags & {"-f", "--force"} or "f" in short):
        return [(_here(pos[1], where), "tree")]
    # a word that names a file and no commit is a path (git checkout main beside a main/ folder is the branch)
    paths = [a for a in pos if os.path.exists(os.path.join(where, a))
             and not _is_commit(where, a)] if sub == "checkout" else []
    staged = "--staged" in flags or "S" in short
    if (sub == "reset" and flags & {"--hard", "--merge"}
            or sub == "checkout" and (flags & {"--force", "--", "."} or "f" in short or paths)
            or sub == "restore" and (not staged or "--worktree" in flags or "W" in short)
            or sub == "clean" and ("--force" in flags or "f" in short)
            and not ("--dry-run" in flags or "n" in short)
            or sub == "switch" and (flags & {"--force", "--discard-changes"} or "f" in short)
            or sub == "rm" and ("--force" in flags or "f" in short)
            or sub in ("merge", "rebase", "cherry-pick", "revert", "am") and "--abort" in flags):
        return [(where, "tree")]
    return []


# T-1482: (folder, what) of every discarding git command the last Bash check read, however it was spelled (\git, $'git',
# a variable, eval, a function, sh <<<): the hook saves these too, where discards_work reads only the plain words
_DISCARDS = []
_DISCARDS_OF = [None]  # the command they were read from
_DCWD = {}  # T-1727 review: depth -> the folders the check at that depth may be in, for the text it reads nested
_COMMITS = {}  # (folder, word) -> whether it names a commit: the guard and the hook ask once per check


def take_discards(cmd=None):
    """What the last Bash check read (and clears it); None when that check wasn't of cmd (an older hook asks without)."""
    if cmd is not None and cmd != _DISCARDS_OF[0]:
        return None
    out, _DISCARDS[:], _DISCARDS_OF[0] = list(_DISCARDS), [], None
    return out


def _is_commit(where, word):
    key = (os.path.abspath(where), word)
    if key not in _COMMITS:
        if len(_COMMITS) > 256:  # a long-lived caller (fm mcp) never checks through findings, which clears it
            _COMMITS.clear()
        _COMMITS[key] = bool(_git(where, "rev-parse", "--verify", "-q", f"{word}^{{commit}}"))
    return _COMMITS[key]


_DISK = {"fdisk", "sfdisk", "cfdisk", "parted", "gdisk", "sgdisk", "wipefs", "mkswap"}
_POWER = {"shutdown", "reboot", "poweroff", "halt"}
_SYSTEMCTL_MUTATE = {"start", "stop", "restart", "reload", "try-restart", "reload-or-restart", "enable", "disable",
                     "reenable", "mask", "unmask", "kill", "isolate", "daemon-reload", "daemon-reexec", "set-default",
                     "poweroff", "reboot", "halt", "suspend", "hibernate", "edit", "set-property", "link", "revert",
                     "preset", "reset-failed"}


_FM_VALUED = {"--timeout", "--repeat", "--evidence", "--step", "--ac", "--expect", "--status", "--json-key", "-p",
              "--project", "--tail", "--top", "--text", "--section", "--file", "--run", "--run-file", "--verify", "--check", "--same",
              "--stack-check", "--good", "--why", "--audit", "--lens", "--docs", "--lesson", "--commit", "--type",
              "--tier", "--title", "--source", "--until", "--model", "--max", "--proof"}
NOTIFY_PROBE = "rm -rf ~"  # what $1 is read as: a notify command that runs its message as code is refused


def _fm_words(rest):
    """T-0901 review: an fm call's positional words, its options (and their values) left out, as argparse reads them."""
    out, i = [], 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            return out + rest[i + 1:]
        if a.startswith("-") and len(a) > 1:
            i += 2 if a in _FM_VALUED else 1
            continue
        out.append(a)
        i += 1
    return out


BUILDER_CHECK_READ = ("run", "list", "office", "affected")  # T-4785: the sampo check verbs that change no gate; every
# other (and any the parser gains later) is refused to a builder: tests/test_guard_exec_vectors ties this to the parser


CHECK_BOOL = frozenset({"-h", "--help", "--json", "--clear", "--fresh", "--fail-fast", "--affected", "--lock", "--hold",
                        "--shape-green"})  # the options of sampo check that take no value
CHECK_VALUED = frozenset({"-p", "--project", "--cell", "--sheets", "--contains", "--for", "--python", "--timeout",
                          "--repeat", "--evidence", "--since", "--step", "--ac"})  # and the ones that take the next word
# T-4785 review: the guard imports nothing of Sampo, so these two tables are tied to the parser by a test that walks its
# options (test_guard_exec_vectors.BuilderCheckConfine); an option in neither is unknown, and an unknown option is refused


def check_args(rest):
    """T-4785 review: (positional words, the options set, the options the guard doesn't know) of `sampo check <rest>`,
    read as argparse reads them: a valued option takes the next word as its value (`--python list rm 1` has the words
    rm, 1: list is a version), so the verb is the first word left. An unknown option (a new one, an abbreviation
    argparse would take, -pX) leaves the words unreadable: the caller refuses on it. The one reading of these words,
    for the builder rule here and the wide-check rule (hooks._builder_check_deny)."""
    words, flags, bad, i = [], set(), [], 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            return words + rest[i + 1:], flags, bad
        if a.startswith("-") and len(a) > 1:
            name = a.partition("=")[0] if a.startswith("--") else a
            if name in CHECK_VALUED:
                flags.add(name)
                i += 1 if "=" in a and a.startswith("--") else 2
                continue
            if name in CHECK_BOOL:
                flags.add(name)
            else:
                bad.append(a)
            i += 1
            continue
        words.append(a)
        i += 1
    return words, flags, bad


def _builder_check(rest):
    """T-4785: why a builder session may not run `sampo check <rest>`, or ''. A builder reads and runs the gates; add, rm,
    paths, ambient, onwrite, playbooks (ratchet) and an affected template change what a lane must pass, and a lane that
    removes its own gate lands unproven. The verb is the first positional word (none: run); `affected` only when it
    shows the template (no command, no --clear or --for). An option the guard doesn't know (CHECK_BOOL, CHECK_VALUED)
    is refused: it could take the next word as its value, and the verb would be a different word. So is any word after
    `check`, an option, its value or a positional, that the shell may fill in or split (an expansion, a substitution, a
    brace list, a glob, a process substitution; _unliteral): `--python $V list` reads as the verb list, while V holding
    `x rm 1` makes it `--python x rm 1 list`. A quoted "$V" is refused too: one word, but its content is unknown and the
    guard reads no quotes. It is the builder's rule alone (the caller), not the user's or the main thread's."""
    if any(_unliteral(w) or re.search(r"[*?\[]|[<>]\(", w) for w in rest):
        return "sampo check with a word the shell fills in or splits at run time (the guard can't read which gate verb it is)"
    words, flags, bad = check_args(rest)
    verb = words[0] if words else "run"
    if bad:
        return "sampo check with an option the guard doesn't know (it can't tell which word is the gate verb)"
    if any(_unresolvable(w) for w in words):
        return "sampo check with a word the shell fills in at run time (the guard can't read which gate verb it is)"
    if verb not in BUILDER_CHECK_READ or (verb == "affected" and (len(words) > 1 or {"--clear", "--for"} & flags)):
        return f"sampo check {verb} changes the project's gates"
    return ""


def _fm_stored(sub, rest, fm_args):
    r"""T-0901: the commands an fm call stores, or runs from a word the hook would otherwise never read: a verify
    (`--ac "x :: CMD"`, `--verify`, `fm task ac … "x :: CMD"`, an Acceptance criteria text's `verify with \`CMD\``,
    an intake `DONE-WHEN: x :: CMD`), `fm task assume --check`, `--same`, `--stack-check`, `fm check add`/`affected`
    ({tests} and {names} read as one word), `fm smoke set cmd`, `fm notify` ($1 read as NOTIFY_PROBE), and the CMD of
    `fm record` / `fm whyred`. A capture's text runs nothing, so it isn't one."""
    out, words = [], _fm_words(rest)
    section = ""
    for a, b in zip(fm_args, fm_args[1:] + [""]):  # fm refuses abbreviated options, so these are the spellings
        flag, eq, v = a.partition("=") if a.startswith("--") else (a, "", "")
        v = v if eq else b
        if flag in ("--verify", "--same", "--stack-check") or (flag == "--check" and words[:1] == ["assume"]):
            out.append(v)
        elif flag == "--ac" and " :: " in v:
            out.append(v.rpartition(" :: ")[2])
        elif flag == "--proof" and (m := re.match(r"(?is)\s*num\s*:.*?\bvia\s+(.+)$", v)):
            out.append(m.group(1))  # T-4230: sampo concerns show's number proof runs this argv
        elif flag == "--section":
            section = v
        elif flag == "--text" and words[:1] == ["set"] and section.lower().startswith("acceptance"):
            out += re.findall(r"verify with `([^`]+)`", v)
    if sub == "task" and words[:1] == ["ac"]:  # task ac ID ACTION [N] TEXT: the id and action store nothing
        out += [w.rpartition(" :: ")[2] for w in words[3:] if " :: " in w]
    if sub == "intake":
        out += [ln.rpartition(" :: ")[2] for w in words for ln in w.splitlines()
                if " :: " in ln and re.match(r"\s*(DONE-WHEN|ACCEPT)\s*:", ln, re.I)]
    if sub == "check" and words[:1] in (["add"], ["affected"]) and words[1:]:
        out.append(re.sub(r"\{(tests|names)\}", "tests", " ".join(words[1:])))
    if sub == "smoke" and words[:2] == ["set", "cmd"] and words[2:]:
        out.append(" ".join(words[2:]))
    if sub == "notify" and words:
        out.append(re.sub(r"\$\{?1\}?", NOTIFY_PROBE, " ".join(words)))
    if sub in ("record", "whyred") and words:
        out.append(words[0])
    return [x for x in dict.fromkeys(out) if x.strip()]


def _fm_runs(sub, rest, fm_args):
    """The commands an fm call runs or keeps to run from its own words: _fm_stored's (T-0901), and the trailing command
    of quiet and at (T-0898: fm quiet -- rm -rf ~ used to pass)."""
    words = _fm_trailing(rest, 1 if sub == "at" else 0) if sub in FM_TRAILING else []
    return _fm_stored(sub, rest, fm_args) + (
        [words[0] if len(words) == 1 else " ".join(map(shlex.quote, words))] if words else [])


def _fm_trailing(rest, positionals):
    """T-0898: the command words after `fm quiet` / `fm at TARGET` options (argparse's REMAINDER, or after --)."""
    i, seen = 0, 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            return rest[i + 1:]
        if a in ("--tail", "--timeout", "-p", "--project"):
            i += 2
        elif a.startswith("-"):
            i += 1
        elif seen < positionals:
            seen, i = seen + 1, i + 1
        else:
            return rest[i:]
    return []


def _fm_subcommand(args, takes_value=("-p", "--project"), twins=True):
    """(first word that isn't an option, what follows it); `takes_value` options consume the next word. T-2745: a verb
    twin (rune, louhi, ward, smith, moons) reads as the fm verb it renames."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in takes_value else 1
    if i >= len(args):
        return None, []
    if twins and args[i] in VERB_TWINS:
        old = VERB_TWINS[args[i]].split()
        return old[0], old[1:] + args[i + 1:]
    return args[i], args[i + 1:]


_FM_WORD = lazy(r"(?<![\w.-])" + FM_NAME_RE + r"(?![\w.-])")
_FM_SUBST = r"(?:\$\(|`)[^)`]*?(?<![\w.-])" + FM_NAME_RE + r"(?![\w.-])"  # $(command -v sampo), `which fm`


def _fm_by_subst(text, word, shell):
    """T-3600: a computed command word is sampo's: `text` (its words) holds a substitution naming sampo, or the word is a
    variable the line sets from one (S=$(command -v sampo); $S)."""
    m = re.match(r"\$\{?(\w+)", word)
    return bool(re.search(_FM_SUBST, text) or m and re.search(rf"(?<![\w$]){m[1]}=[\"']?" + _FM_SUBST, shell))


def _fm_known(args, known, xargs=None):
    """T-3600: an fm call's words through the line's known values, a resolved one split as bash splits it unquoted (a
    word holding a space was quoted: kept whole, so a --run text stays one command); xargs adds a word the guard can't
    read at the end, and in front too when an option may replace any word (-I)."""
    out = []
    for a in args:
        v = _with_vars(a, known)
        out += v.split() if v != a and not _unresolvable(v) and not re.search(r"\s", a) else [v]
    return out if xargs is None else (["$(xargs)"] if xargs else []) + out + ["$(xargs)"]


def _loop_body(text, after):
    """T-4785 round 8: (start, end) of the loop body from `after` (just past its `do`) to the `done` that closes it,
    counting the loops inside it, read on the quote-masked text; None when it never closes."""
    masked, depth = _mask_quotes(text), 1
    for m in re.finditer(r"(?<=[;\n&|({])[ \t]*(do|done)(?![\w-])", masked[after:]):  # a keyword, not an argument (echo do)
        depth += 1 if m[1] == "do" else -1
        if not depth:
            return after, after + m.start(1)
    return None


def _loop_values(word, holds):
    """T-3600: (V, words) when `word` is $V and the whole command gives V only by one `for V in …` loop over literal
    words, naming $V nowhere before it, on a line whose literals hold (_literals_hold); (None, None) otherwise.
    T-4785 round 8: and only when every $V on the line sits between that loop's `do` and its matching `done`, a body
    with no `(`, `{`, pipe or `&` (a use after the loop, in a subshell, a pipeline stage or a branch that never ran is
    a use of a name this loop never gave). Not read for a sampo call's verb chain at all (_fm_chain_unread)."""
    m = re.fullmatch(r"\$(?:\{(\w+)\}|(\w+))", word)
    text = _DCWD.get("line", "")
    v = m and (m[1] or m[2])
    loops = list(re.finditer(rf"(?<![\w$-])for\s+{v}\s+in\s+([^;\n]*?)\s*[;\n]\s*do\b", text)) if v and holds else []
    if len(loops) != 1 or re.search(rf"\$\{{?{v}(?!\w)", text[:loops[0].start()]):
        return None, None
    body = _loop_body(text, loops[0].end())
    if not body:
        return None, None
    inside = _mask_quotes(text)[body[0]:body[1]]
    if re.search(r"[(){|&]", re.sub(r"\$\{\w+\}", "", inside)) or len(re.findall(
            rf"\$\{{?{v}(?!\w)", text)) != len(re.findall(rf"\$\{{?{v}(?!\w)", text[body[0]:body[1]])):
        return None, None
    try:
        words = shlex.split(_with_vars(loops[0][1], _fm_values(_DCWD.get("lits", {}))))
    except ValueError:
        return None, None
    return (v, words) if words and all(re.fullmatch(r"[\w./@:+,=% -]+", w) for w in words) else (None, None)


_INERT_BUILTINS = frozenset("echo test [ true false : pwd cd".split())  # bind no name, run no code
_HEADERS = frozenset("for case select in [[ ]] coproc function".split())  # a keyword whose words are no command


def _literals_hold(cmds, shell, vals):
    """T-3600 review 4: whether a name this line sets to a literal still holds where a sampo word reads it. Only code
    running in this shell rebinds a name (a builtin, a function, a sourced file, a trap, a computed command word); an
    external program runs in a child and can't. So each command word here must be an external program, an inert
    builtin (_INERT_BUILTINS), a keyword other than function, coproc or select, or NAME=value; a for variable is
    read as its literal words when nothing else sets it. Anything else: the line's literals are not used."""
    loops = {}
    for m in re.finditer(r"(?<![\w$-])for\s+(\w+)\s+in\s+([^;\n]*?)\s*[;\n]\s*do\b", shell):
        loops.setdefault(m[1], []).extend(_with_vars(m[2], vals).split())
    masked = _mask_quotes(shell)
    if any(re.search(rf"(?<![\w$]){v}(?:\[[^]]*\])?\+?=", masked) for v in loops) or re.search(
            r"(?:^|[\s;&|({])(?:function\s+[^\s(]+|[^\s;&|()<>=$`]+\s*\(\s*\))", masked):
        return False  # a for variable set again, or a function defined here
    for c in cmds:
        words = list(c.argv)
        while words and (_ASSIGNISH.match(words[0]) or words[0] in BASH_KEYWORDS and words[0] not in _HEADERS):
            words = words[1:]
        if not words or words[0] in _HEADERS:
            if words and words[0] in ("function", "coproc", "select"):
                return False
            continue
        word, var = _with_vars(words[0], vals), re.fullmatch(r"\$\{?(\w+)\}?", words[0])
        names = loops[var[1]] if var and var[1] in loops else [] if _unresolvable(word) else word.split()[:1]
        if not names or any(n in BASH_BUILTINS and n not in _INERT_BUILTINS or _unresolvable(n) for n in names):
            return False
    return True


def _fm_values(vals):
    """T-3600 review 2: the values a sampo word may be read through, without one that is itself a name the command
    names again (declare -n r=V; V=serve: $r is V's value, however the setter is spelled: \\declare, "declare", $d,
    command declare). A value that is a name nowhere else on the line is a value (V=next)."""
    line = _DCWD.get("line", "")
    return {k: v for k, v in vals.items() if not (re.fullmatch(r"[A-Za-z_]\w*", v) and len(re.findall(
        rf"(?<!\w){v}(?!\w)", line)) > 1)}


def _fm_unread(args):
    """T-3600: (the first word of an fm call the shell fills in at run time (_unliteral) where the guard keys a refusal
    on it, "verb" or "run"): at or before the verb (an option word may take the next as its value, a split one may
    become any verb), among a gated verb's later words, or an --allow value ("verb"); a command the call runs or keeps
    whose text holds one ("run"); (None, None) when there is none. A quoted word holding a space before any expansion
    is one word starting with literal text, so it can't become a gated word."""
    _, rest = _fm_subcommand(args, twins=False)
    sub = _fm_subcommand(args)[0]
    gated = [w for w in rest if not re.match(r"[^$`\s]*\s", w)] if sub in USER_ONLY_FM or sub in FM_GATED else []
    allows = [a.partition("=")[2] or b for a, b in zip(args, args[1:] + [""]) if _is_allow(a)]
    verb = next((w for w in args[:len(args) - len(rest)] + gated + allows if _unliteral(w)), None)
    run = None if verb else next((r for r in _fm_runs(*_fm_subcommand(args), args) if _RUN_COMPUTED.match(r)), None) or \
        next((w for i, w in enumerate(args) if _fm_runs_word(args, i, sub)), None)
    return (verb, "verb") if verb else (run, "run") if run else (None, None)


def _fm_chain_unread(args):
    """T-4785 round 7: the first word in a sampo call's verb chain, read as typed, that the shell fills in: an option or
    the verb, a gated verb's later words, an --allow value (the positions _fm_unread keys a refusal on). No value the
    line sets is read here: a credited value had to match what the shell runs through a prefix, a branch, a child, an
    env -i, a nested re-assignment, and each of those was a way for the two to differ. Round 8: a `for` loop's words
    are no exception (a loop in a branch that never runs, a pipeline stage or a subshell gave the verb a value the
    shell never ran)."""
    _, rest = _fm_subcommand(args, twins=False)
    sub = _fm_subcommand(args)[0]
    gated = [w for w in rest if not re.match(r"[^$`\s]*\s", w)] if sub in USER_ONLY_FM or sub in FM_GATED else []
    allows = [a.partition("=")[2] or b for a, b in zip(args, args[1:] + [""]) if _is_allow(a)]
    head = args[:len(args) - len(rest)] + rest[:1] * bool(gated)  # a glob (ris?) is a file name here, so the verb or sub-verb

    def filled(w):  # an expansion, a brace list, or in the verb positions a glob
        return _unliteral(w) or w in head and not re.search(r"\s", w) and bool(re.search(r"[*?\[]", w))
    return next((w for w in head + gated + allows if filled(w)), None)


_DOLLAR_OR_TICK = lazy(r"[$`]")
_UNREAD_REST = ["$__u"]  # the words after a computed command word, when they can't be found: an unreadable one, refused


def _strip_expansions(word):
    """(word without its ${…}, $(…) (balanced) and `…`, whether one of them never closed: it ran to the end)."""
    out, i, n, opened = [], 0, len(word), False
    while i < n:
        ch = word[i]
        if ch == "$" and word[i + 1:i + 2] in ("{", "("):
            o = word[i + 1]
            c, depth, i = "}" if o == "{" else ")", 1, i + 2
            while i < n and depth:
                depth += (word[i] == o) - (word[i] == c)
                i += 1
            opened = opened or bool(depth)
        elif ch == "`":
            j = word.find("`", i + 1)
            opened = opened or j < 0
            i = n if j < 0 else j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), opened


def _is_debris_word(word, shell):
    """A "command word" holding a space or an operator once its expansions are taken out is no word bash would run: the
    quote-blind fallback split cut it out of a quote it misplaced (a real quoted one, "${x#*;}", keeps its operators
    inside the expansion). Only a line that fallback split can hold one."""
    return bool(re.search(r"[;&|\s]", _strip_expansions(word)[0])) and _shlex_fails(_lines(shell))


def _computed_rests(cmds, idx, argv):
    """T-4785 round 9 (b): the words the shell hands a computed command word argv[0] that no credited literal joins, each
    reading to try as sampo's own: what the tokenizer (quote-aware) put after it, so no second scan of the text. A bare $
    is an unquoted $( ): its body's commands follow, then the ")" command holding the words after it (and the one glued to
    it, so also without the first); a body with a case reads each ")" up to the last. Where the words can't be found, or a
    word's own $( or backtick never closes (the fallback split), every suffix of them: a refused one is refused."""
    name, args = argv[0], argv[1:]
    if name == "$" and idx + 1 < len(cmds) and cmds[idx + 1].op.startswith("("):
        # the tokenizer gave each command the paren depth it stands at: the body is the run deeper than the stub, and the
        # first command back at the stub's depth is what follows the ")" (its words, if the op is the ")" itself; no
        # words, if an operator closed it: that command is a fresh one). A case in the body has ")" of its own: every
        # later command at that depth is tried
        d, out, case = cmds[idx].depth, [], False
        for c in cmds[idx + 1:]:
            if c.depth > d:
                case = case or "case" in c.argv or ";;" in c.op
                continue
            ends = c.op.endswith(")")
            out += [c.argv, c.argv[1:]] if ends else [[]]
            if not case:
                return out
            if len(out) > 32:  # too many to try: the one it can't read
                return out + [_UNREAD_REST]
        return out + [[]]  # the line ended right after the ")": the rest is nothing
    if name == "$":  # a bare $ word, no substitution after it
        return [args]
    if _strip_expansions(name)[1]:
        return [args[i:] for i in range(min(len(args), 32) + 1)] + [_UNREAD_REST] * (len(args) > 32)
    return [args]


_NET_VERBS = frozenset(USER_ONLY_FM) | FM_GATED | frozenset(FM_STORING)  # the tables only


def _stub_rests(cmds, idx):
    """T-4785 round 11: [(word, literal words after it)] for each expansion word ($(…) or `…` or $x) among cmds[idx]'s
    words, wherever the command stands: the words that follow it up to the next expansion, when every one is a plain
    literal. A bare $ ends its command (the body follows as the commands after it): what follows is what the ")" holds
    (_computed_rests). A backtick span the split cut in pieces ends at the piece that closes it."""
    argv, out, n = cmds[idx].argv, [], len(cmds[idx].argv)
    isx = [bool(_DOLLAR_OR_TICK.search(w)) for w in argv]
    nx, od = [n] * (n + 1), [n] * (n + 1)  # from the right: the first expansion, the first odd-backtick word, at or after i
    for i in range(n - 1, -1, -1):
        nx[i] = i if isx[i] else nx[i + 1]
        od[i] = i if argv[i].count("`") % 2 else od[i + 1]
    for j, w in enumerate(argv):  # linear: a start index stands for each rest, and only a rest that opens on a verb is cut
        if not isx[j]:
            continue
        if w == "$":
            rests = _computed_rests(cmds, idx, argv[j:]) if j == n - 1 else [None]
            starts = [None if r is not None else j + 1 for r in rests]
        elif w.count("`") % 2:
            k = od[j + 1]
            starts = [] if k == n or w[-1:] == "`" and not w.startswith("`") else [k + 1]
            rests = [None] * len(starts)
        else:
            starts, rests = [j + 1], [None]
        for start, rest in zip(starts, rests):
            if start is not None:
                rest = argv[start:nx[start]] if start < n and argv[start] in _NET_VERBS else []
            elif rest and rest[0] in _NET_VERBS:  # a rest the ")" holds, already a list of its own
                rest = rest[:next((i for i, x in enumerate(rest) if _DOLLAR_OR_TICK.search(x)), len(rest))]
            else:
                rest = []
            if rest and rest[0] in _NET_VERBS:  # a literal verb chain only: an unknown word is the (b) reading's
                out.append((w, rest))
    return out


def _is_argument_stub(cmds, idx):
    """Whether the bare $ that starts cmds[idx] is an unquoted $( ) standing among the words of a command: it follows the
    ")" of an earlier one (echo $(a) $(b): the ")" is the op). One after a ";" or a case pattern's ")" starts a command; so
    does one when a case lies in the run between (its pattern's ")" looks as a substitution's). Round 10: only when the
    command that owns the earlier substitution already has a command word of its own (after assignments and wrappers):
    x=$(a) $(b) and >$(a) $(b) have none, so the second one is it. Whatever can't be followed is a command word."""
    c, words = cmds[idx], []
    if c.argv[:1] != ["$"] or not c.op.endswith(")") or c.op.endswith(");") or idx == 0:
        return False
    j = idx
    while True:  # the command that owns each earlier substitution, and the ones continuing it after a ")"
        k = j
        while k > 0 and cmds[k - 1].depth > cmds[j].depth:
            k -= 1
        run = cmds[k:j]
        if not run or k == 0 or not run[0].op.endswith("(") or any("case" in r.argv or ";;" in r.op for r in run):
            return False
        j = k - 1
        words = cmds[j].argv + words
        if not cmds[j].op.endswith(")") or cmds[j].op.endswith(");"):
            break  # the head of the command
    return bool(_strip_wrappers(words)[0])


def _read_as_sampo(rest, ctx, depth):
    """The findings of the line `sampo <rest>` (its words as _typed_args keeps them), judged once a line."""
    text = " ".join(["sampo"] + _typed_args(rest))
    memo = _DCWD.setdefault("asfm", {})
    if text not in memo:
        memo[text] = check_bash(text, ctx, depth + 1, tails=False, at=ctx.cwd)
    return memo[text]


def _typed_args(args):
    """T-4785 round 9: a call's words for a reading of the line as another command: a word with no shell syntax as it is, one
    with an expansion cut at it and ended with a bare $__u (the verb-chain check keys on the expansion, which a quoted $
    would hide), any other quoted."""
    out = []
    for a in args:
        if re.fullmatch(r"[\w${}.:=@/+,%^-]*", a) and not re.search(r"\$\{[^}]*$", a):
            out.append(a)
        elif (m := re.search(r"[$`]", a)):
            out.append((shlex.quote(a[:m.start()]) if m.start() else "") + "$__u")
        else:
            out.append(shlex.quote(a))
    return out
_FM_PROBE = "DONE-WHEN: x :: \0 verify with `\0`"  # a word every reader in _fm_stored takes as a command
_BINDS = lazy(r"\bfor\s+(\w+)\s+in\b|\bread\s+(?:-\S+\s+)*(\w+)|(?<![\w$])(\w+)\+?=")  # names a text sets itself
_DOLLAR = lazy(r"(?<!\\)\$(?:\{[^}]*\}?|\([^)]*\)?|\w+|.?)")  # a $ expansion: $X, ${…}, $( ), $1, $@
# a command fm runs or keeps whose command word the shell fills in (after NAME=value words): "$C", `cmd` args
_RUN_COMPUTED = lazy(r"\s*(?:[A-Za-z_]\w*=\S*\s+)*[\"']?[$`]")
_FM_RUN_WORD = lazy(r"(?<![\w.-])" + FM_NAME_RE + r"(?![\w./-])")  # sampo, not a sampo/ folder in a path


def _fm_runs_word(args, i, sub):
    """T-3600 review: whether what the shell fills into args[i] may land in a command the call runs or keeps
    (_fm_runs: task ac T-1 add "$X" with X='x :: CMD'): each expansion in it is read as a probe those readers take as
    a command, so a verb or flag added there is covered. notify's own $1 is its message, read as NOTIFY_PROBE there and
    here. ponytail: a `…` is read as written (it is also verify with's own syntax, and markdown's); its body is read as
    a command, its output landing in a stored command isn't."""
    w = re.sub(r"\$\{?1\}?", NOTIFY_PROBE, args[i]) if sub == "notify" else args[i]
    # a stored command's own code is read as code: a name it binds itself (for n in, read n, n=) and a $( ) inside
    # longer text; a word that is all one expansion is unknown whatever it is. ponytail: so "pytest $(cat f)" passes
    whole = bool(_DOLLAR.fullmatch(w))
    own = set() if whole else {n for t in _BINDS.findall(w) for n in t if n}

    def keep(m):
        name = re.match(r"\$\{?(\w+)", m[0])
        return not whole and (m[0][:2] == "$(" or bool(name) and name[1] in own)
    probe = _DOLLAR.sub(lambda m: m[0] if keep(m) else _FM_PROBE, w)
    if probe == w:
        return False
    probe = args[:i] + [probe] + args[i + 1:]
    return any("\0" in _mask_quotes(r) for r in _fm_runs(*_fm_subcommand(probe), probe))  # as code, not a quoted
    # argument (quiet's several words are argv: sampo quiet -- git diff "$REF")


def _fm_argv(argv):
    """argv (wrappers already off) runs Sampo's fm: `fm`/`sampo` by any path, or python running plugin/bin/<name>."""
    name = os.path.basename(argv[0]) if argv else ""
    return name in FM_PROGRAMS or bool(re.match(r"^python[0-9.]*$", name)) and any(
        a.endswith(FM_SCRIPT_ENDS) for a in argv[1:2])


def _fm_named_elsewhere(shell, cmds):
    """T-3600 review: the line names sampo more often than it runs it (read C <<< 'sampo serve', f sampo serve, a
    string): a command word the shell fills in there may be sampo, with a verb the guard can't read. T-4922: not a
    sampo word that is a grep or rg pattern (_fm_pattern_texts), text to find, not a command the line can fill in."""
    for prog, text in _fm_pattern_texts(cmds):
        # the text as written, in the words after the grep that starts a command: a pattern quoted in pieces
        # ('s''ampo') stays counted, and so does a sampo word in a string the line holds
        for m in re.finditer(rf"(?:^|[;&|({{\n!]|\b(?:then|do|else|elif|if|while|until)\b)\s*(?:\S*/)?{prog}\b((?:'[^']*'|\"[^\"]*\"|[^;&|\n)'\"])*)",
                             shell):
            if text in m[1]:
                at = m.start(1) + m[1].index(text)
                shell = shell[:at] + " " + shell[at + len(text):]
                break
    return len(_FM_RUN_WORD.findall(shell)) > sum(1 for x in cmds if _fm_argv(_strip_wrappers(x.argv)[0]))


_GREPS = ("grep", "egrep", "fgrep", "zgrep", "rg")
_GREP_VALUED = ("-m", "-A", "-B", "-C", "-d", "-D", "-g", "-t", "-T", "--max-count", "--after-context", "--before-context",
                "--context", "--glob", "--type", "--type-not", "--include", "--exclude", "--exclude-dir")
_GREP_FILTERS = ("grep", "egrep", "fgrep", "zgrep", "rg", "head", "tail", "sort", "uniq", "wc", "cut", "tr", "cat", "nl",
                 "tac", "rev", "column")  # what a grep's lines may be piped to: none runs or keeps them as a command


def _fm_pattern_texts(cmds):
    """T-4922: the pattern words of the grep and rg commands whose output only reaches the terminal or a filter. A
    pattern naming sampo (sed … log | grep -E 'sampo|FAIL') finds text; it is no command the shell runs, nor one a
    computed word on the line could become. Not when the output may be read back (-o prints just the match, a > file or
    tee keeps it, a shell, read or xargs after it runs it), a pattern with an expansion in it, or a line with a
    process substitution."""
    if any(x.procsub for x in cmds):
        return []
    out = []
    for i, c in enumerate(cmds):
        argv = _strip_wrappers(c.argv)[0]
        prog = os.path.basename(argv[0]) if argv else ""
        if prog not in _GREPS or any(r != "/dev/null" for r in c.redirs):
            continue
        tail = []
        for x in cmds[i + 1:]:
            if not x.piped:
                break
            tail.append(_strip_wrappers(x.argv)[0][:1])
        if any(not t or os.path.basename(t[0]) not in _GREP_FILTERS for t in tail):
            continue
        out += [(prog, p) for p in _grep_patterns(prog, argv[1:])]
    return [(r, p) for r, p in out if _FM_RUN_WORD.search(p) and not re.search(r"[$`]|<\(", p)]


def _grep_patterns(prog, args):
    """The pattern words of a grep or rg call: the -e/--regexp values, else the first word that isn't an option or its
    value. [] when it prints only the match (-o) or its pattern comes from a file (-f), and for rg's -r (it prints the
    replacement text); a word it can't place is left counted (grep -m 5 sampo takes 5, not sampo)."""
    pats, plain, i, ended = [], [], 0, False
    while i < len(args):
        a = args[i]
        if ended or not a.startswith("-") or a == "-":
            plain.append(a)
        elif a == "--":
            ended = True
        elif a.startswith("--"):
            name, eq, val = a.partition("=")
            if "--only-matching".startswith(name) and len(name) >= 4 or name in ("--file", "--replace"):
                return []
            if name == "--regexp":
                pats.append(val if eq else args[i + 1] if i + 1 < len(args) else "")
                i += 0 if eq else 1
            elif name in _GREP_VALUED and not eq:
                i += 1
        else:  # a short cluster: -o and -f (or rg's -r) end it; -e takes the next word, a valued option takes its value
            if "o" in a[1:] or "f" in a[1:] or prog == "rg" and "r" in a[1:]:
                return []
            if a.endswith("e"):
                pats.append(args[i + 1] if i + 1 < len(args) else "")
                i += 1
            elif "e" in a[1:]:
                pats.append(a[a.index("e", 1) + 1:])
            elif a in _GREP_VALUED:
                i += 1
        i += 1
    return pats or plain[:1]


_CLAUDE_CHANGES = {"plugin": {"install", "i", "enable", "disable", "uninstall", "remove", "update"},
                   "mcp": {"add", "add-json", "add-from-claude-desktop", "remove"}, "config": {"set", "add", "remove"}}


_SESSION_CONFIG = ("--settings", "--mcp-config", "--plugin-dir")
_UPDATE = "claude plugin update changes Claude Code's plugins, MCP servers or config"  # the details check() pairs
_MARKET_UPDATE = "claude plugin marketplace update"


def _check_claude_config(name, args, stdin=""):
    """Installing or toggling plugins, marketplaces and MCP servers, or changing Claude Code's config: new code and
    always-on context in every session, so only the user's yes to `fm ask ID plugin` allows it. A session started
    with its own settings, MCP servers or plugins counts too (its settings can switch Sampo's hooks off)."""
    if not re.fullmatch(r"claude(?:-code)?(?:@[\w.-]+)?", name):  # also npx @anthropic-ai/claude-code[@version]
        return []
    # a prompt's slash command isn't a tool call the guard sees; `stdin` is the raw command text when claude reads a
    # pipe or heredoc (echo … | claude -p, claude -p <<EOF)
    if _SLASH_CHANGE.search(stdin) or any(_SLASH_CHANGE.search(a) for a in args):
        return [("plugin", "a /plugin, /mcp or /config change sent to claude as a prompt" + plugin_mark("?"))]
    flags = [a.split("=", 1)[0] for a in args if a.split("=", 1)[0] in _SESSION_CONFIG]
    if flags:
        return [("plugin", f"claude {flags[0]} starts a session with its own settings, MCP servers or plugins")]
    # every adjacent pair, not just the first two words: global options and their values can come first
    pos = ["plugin" if a == "plugins" else a for a in args if not a.startswith("-")]
    for a, b, c3 in zip(pos, pos[1:], pos[2:] + [""]):
        if (a, b) == ("plugin", "marketplace") and c3 in ("add", "remove", "rm", "update"):
            return [("plugin", f"claude plugin marketplace {c3}")]
        if b in _CLAUDE_CHANGES.get(a, ()):
            target = plugin_mark(_claude_plugin_target(args)) if a == "plugin" and b in _PLUGIN_ADDS else ""
            return [("plugin", f"claude {a} {b} changes Claude Code's plugins, MCP servers or config{target}")]
    return []


_PLUGIN_ADDS = ("install", "i", "enable")  # the plugin changes that bring code in: a yes for them is pinned (T-0036)
PLUGIN_ID = lazy(r"[\w.-]+(?:@[\w.-]+)?")  # name or name@marketplace; fm ask --pin takes the same


def plugin_mark(target):
    """The end of a plugin finding's detail naming the plugin it installs or enables ("?" when it can't tell); the
    hook reads it back with plugin_target, the only other place that knows this format."""
    return f" [plugin {target}]"


def plugin_target(detail):
    m = re.search(r" \[plugin (\S+)\]$", str(detail))
    return m.group(1) if m else None


def _one_plugin(words):
    """The plugin id when words name exactly one, else "?": no yes is spent on a change it can't check (T-0036)."""
    return words[0] if len(words) == 1 and PLUGIN_ID.fullmatch(words[0]) else "?"


def _claude_plugin_target(args):
    """The plugin a `claude plugin install|i|enable …` names (the value of -s/--scope isn't one), or "?"."""
    for i, (x, y) in enumerate(zip(args, args[1:])):
        if x in ("plugin", "plugins") and y in _PLUGIN_ADDS:
            words, skip = [], False
            for a in args[i + 2:]:
                if skip or a in ("-s", "--scope"):
                    skip = not skip
                    continue
                if not a.startswith("-"):
                    words.append(a)
            return _one_plugin(words)
    return "?"


_USER_UNIT_PERSIST = {"link", "enable", "reenable", "edit", "preset", "revert", "set-property", "add-wants",
                      "add-requires"}


def _check_system(name, args):
    pos = _positionals(args)
    # scheduled or hook code that runs later, outside the session (like a user systemd unit)
    if name == "crontab" and not set(args) & {"-l", "--list"}:
        return [("system", "crontab change (code that runs on a schedule)")]
    if name in ("at", "batch"):
        return [("system", f"{name} schedules a command to run later")]
    if name == "direnv" and pos[:1] in (["allow"], ["permit"], ["grant"]):
        return [("system", "direnv allow (.envrc runs on every cd into the folder)")]
    if name == "git":
        # hooks from another folder, set for one command (-c, --config-env) or persistently (git config)
        if any(re.match(r"(?i)(--config-env=|-c)?core\.hookspath=", a) for a in args):
            return [("system", "git with core.hooksPath (hooks from another folder)")]
        sub, rest = _fm_subcommand(args, takes_value=("-C", "-c"), twins=False)
        values = [a for a in rest if not a.startswith("-")]
        if sub == "config" and [v.lower() for v in values[:1]] == ["core.hookspath"] and len(values) > 1 \
                and not set(rest) & {"--get", "--get-all", "--list", "-l", "--unset", "--unset-all"}:
            return [("system", "git core.hooksPath (hooks that run on every git command)")]
    if name.startswith("mkfs") or name in _DISK:
        return [("system", f"{name} (disk/partition change)")]
    if name == "dd" and any(a.startswith("of=/dev/") and not _SAFE_DEV.match(a[3:]) for a in args):
        return [("system", "dd onto a device")]
    if name in ("iptables", "ip6tables", "iptables-restore", "ip6tables-restore"):
        if not any(a in ("-L", "--list", "-S", "--list-rules") for a in args):
            return [("system", f"{name} firewall change")]
    if name == "nft" and pos[:1] != ["list"]:
        return [("system", "nft firewall change")]
    if name == "ufw" and pos[:1] not in (["status"], ["show"]):
        return [("system", "ufw firewall change")]
    if name == "firewall-cmd" and not all(a.startswith(("--list", "--get", "--state", "--query", "--zone")) for a in args):
        return [("system", "firewall-cmd change")]
    if name == "systemctl" and "--user" not in args and pos and pos[0] in _SYSTEMCTL_MUTATE:
        return [("system", f"systemctl {pos[0]} (system unit)")]
    if name == "systemctl" and "--user" in args and pos and pos[0] in _USER_UNIT_PERSIST:
        return [("system", f"systemctl --user {pos[0]} (a user unit outlives the session)")]
    if name == "systemd-run":
        return [("system", "systemd-run (a transient unit outside the session)")]
    if name in _POWER or (name == "init" and pos[:1] in (["0"], ["6"])):
        return [("system", f"{name} (power state)")]
    if name == "cryptsetup" and pos[:1] and pos[0] in ("luksFormat", "erase", "luksErase", "reencrypt"):
        return [("system", f"cryptsetup {pos[0]}")]
    return []


_PUBLISH = {
    "npm": ["publish", "unpublish"], "pnpm": ["publish"], "yarn": ["publish", "npm publish"], "bun": ["publish"],
    "cargo": ["publish", "yank"], "twine": ["upload"], "gem": ["push"], "poetry": ["publish"], "uv": ["publish"],
    "docker": ["push"], "podman": ["push"], "gh": ["release create", "release upload", "release edit"],
    "terraform": ["apply", "destroy"], "tofu": ["apply", "destroy"], "pulumi": ["up", "destroy"],
    "kubectl": ["apply", "delete", "rollout", "replace", "patch", "scale", "create", "set"],
    "helm": ["install", "upgrade", "uninstall", "rollback", "delete"],
    "fly": ["deploy"], "flyctl": ["deploy"], "firebase": ["deploy"], "serverless": ["deploy"], "sls": ["deploy"],
    "wrangler": ["deploy", "publish"], "heroku": ["releases:rollback"],
}
_LIT_ASSIGN = lazy(r"(?<![\w$])([A-Za-z_]\w*)=(?:'([^']*)'|\"([^\"$`\\]*)\"|([^\s;&|()<>'\"$`\\]*))(?=[\s;&|)]|$)")


def _line_literals(shell):
    """T-0587: NAME → value for a name set once on the line to a literal (spaces kept: q='rm -rf') and named nowhere
    else but in $q / ${q}, so no loop, read, declare or second assignment can have changed it."""
    out = {}
    for m in _LIT_ASSIGN.finditer(_mask_quotes(shell)):  # an assignment, not quoted text that looks like one
        k = next(k for k in (2, 3, 4) if m.group(k) is not None)
        name, val = m.group(1), shell[m.start(k):m.end(k)]
        rest = shell[:m.start()] + shell[m.end():]
        if not re.search(rf"(?<![\w$]){name}(?!\w)|\$\{{[#!]?{name}[^}}]", re.sub(rf"\$(?:\{{{name}\}}|{name}(?!\w))", "", rest)):
            out[name] = val
    return out


def _seq_literals(shell, cmds, lits, home=None):
    """T-4785 round 4: the part of _line_literals a sampo word may read: a name's value counts only where its assignment
    runs before every use, in the shell that uses it. _line_literals only knew the name is set once on the line, so
    `(V="status x"); sampo $V risk off`, `sampo $V risk off; V=…`, `false && V=…; sampo $V …` and a V set in a loop or an
    if read as the value while the shell had V unset (and ran `sampo risk off`). The rm path already reads a variable by
    sequence: a straight line command by command (_track_vars), a branchy one by the prefix before its first branch,
    pipe, subshell or keyword (_prefix_vars). This is that reading, values with spaces kept (C="sampo task"), for the
    names _line_literals found, and the assignment textually before the first $name. A name it can't place is no
    literal: the word stays an expansion and the verb rules refuse it."""
    if not lits:
        return {}

    def first(k):  # `echo $S; S=x` runs the use first: the value comes after
        a, r = re.search(rf"(?<![\w$]){k}=", shell), re.search(rf"\$\{{?{k}(?!\w)", shell)
        return a is not None and (r is None or a.start() < r.start())
    if _straight_line(shell) and "`" not in shell and "$(" not in shell:  # a substitution runs in a subshell: its
        raw, env, got, seen = _raw_cmds(shell), {}, {}, set()  # assignments are no replay step (the prefix path cuts there)
        if raw is None or len(raw) != len(cmds):
            return {}
        for words in raw:
            text = " ".join(words)
            for k in lits:  # the value the first command that uses $k sees (one that computes its name ends the replay
                if k not in seen and re.search(rf"\$\{{?{k}(?!\w)", text):  # only after its own uses are read)
                    seen.add(k)
                    if k in env and first(k):
                        got[k] = env[k]
            env = _track_vars(words, env, home, True)
            if env is None:
                break
        return got
    return {k: v for k, v in _prefix_vars(shell, cmds, home, True).items() if k in lits}


def _child_check(text, ctx, depth, handed=None, holds=True):
    """T-4785 round 6: check_bash on text a CHILD PROCESS runs (sh -c, script -c, a here-doc script, a script file,
    python's os.system, a git hook): a child doesn't inherit this shell's unexported variables, so `V=status; sh -c
    'sampo $V risk off'` runs `sampo risk off`. The values this line credits to sampo words (_DCWD["lits"]) are cleared
    for it, except `handed`: the literal assignments the child's own command carries (`V=status sh -c '…'`)."""
    old = _DCWD.get("lits", {}), _DCWD.get("holds"), _DCWD.get("line", "")
    handed = _inherited(handed, text) if handed else {}  # round 7: not a name the text sets again
    _DCWD["lits"], _DCWD["holds"] = handed, holds if handed else True  # its own text's holds are its own
    _DCWD["line"] = text  # round 8: a loop of the parent's line is no loop of the child's text (a child has its own)
    try:
        return check_bash(text, ctx, depth)
    finally:
        _DCWD["lits"], _DCWD["holds"], _DCWD["line"] = old


_ENV_RESETTERS = frozenset("sudo doas su runuser setpriv chroot exec".split())  # a program that drops or replaces the env


def _handed(argv, stripped, lits):
    """The literal values a command's own env prefix (`V=x sh -c …`) gives its child: the words before the program that
    are assignments of a name the line sets once to a literal (so lits holds that very value). T-4785 round 7: none when
    a word dropped after the assignment resets the environment (env -i, env -u V, env -, exec -c, sudo, doas): the child
    never sees V."""
    dropped = argv[:len(argv) - len(stripped)]
    for w in dropped:
        if not _ASSIGN.match(w) and (os.path.basename(w) in _ENV_RESETTERS or w == "-" or w.startswith(
                ("--ignore-environment", "--unset")) or (w.startswith("-") and not w.startswith("--") and re.search(
                    r"[iu]", w))):
            return {}
    out = {}
    for w in dropped:
        m = _ASSIGN.match(w)
        if m and m.group(1) in lits and lits[m.group(1)] == w.partition("=")[2]:
            out[m.group(1)] = lits[m.group(1)]
    return out


def _inherited(lits, text):
    """T-4785 round 7: the names of `lits` that nested text (what a child runs, or an eval's body) still reads through the
    value its parent gave: those it uses only as a plain $name or ${name}. A name it mentions any other way (V=…, read V,
    for V in, declare V, unset V, ${V:=x}, printf -v V) it may set again in a way the sequence reading can't credit
    (`if true; then V=risk; fi`, `V=$(echo risk)`), so the value is dropped and the word stays an expansion."""
    out = {}
    for name, val in lits.items():
        rest = re.sub(rf"\$(?:\{{{name}\}}|{name}(?!\w))", "", text)
        if not re.search(rf"(?<![\w$]){name}(?!\w)|\$\{{[#!]?{name}[^}}]", rest):
            out[name] = val
    return out


# T-0587: what a command name bash computes from an unknown value ($X, `which x`) is checked as: those whose check
# reads the arguments (ponytail: crontab, at, nft, ufw, disk and power tools refuse nearly any arguments, so they'd
# refuse every computed name; a bare `$X` that is shutdown stays unread)
_COMPUTED_AS = ["rm", "git", "claude", "dd", "systemctl", "cryptsetup", "direnv", "init"] + sorted(_PUBLISH)
_RM_LONG = ("force", "interactive", "one-file-system", "no-preserve-root", "preserve-root", "recursive", "dir", "verbose",
            "help", "version")


def _readings(text, vals):
    """T-0587 (its review): text eval or sh -c runs, as written (an unknown $q: empty, conditional, a subshell's) and,
    when it differs, with the literal values this line set: both are read, so a value adds findings, never hides one"""
    out = [text]
    for t in (_with_vars(text, vals), re.sub(r"\\([$`])", r"\1", text)):  # T-0669: and with \$ and \` unescaped, as
        if t not in out:                                                 # double quotes unescape them
            out.append(t)
    return out


def _rm_opts_only(args):
    """T-0587: no option rm would refuse (one makes it exit before deleting). T-0668 (its review): getopt takes any
    prefix of a long option (--rec), and under POSIXLY_CORRECT options end at the first operand, so only the options
    before it count; any doubt reads as rm."""
    for a in args:
        if a == "--" or not a.startswith("-") or a == "-":
            return True
        if a.startswith("--"):
            if not any(o.startswith(a[2:].split("=")[0]) for o in _RM_LONG):
                return False
        elif not re.fullmatch(r"-[fiIrRdv]+", a):
            return False
    return True


def _check_publish(name, args):
    if "--dry-run" in args:
        return []
    if re.match(r"^python[0-9.]*$", name) and args[:1] == ["-m"] and len(args) > 1:
        name, args = args[1], args[2:]
    if re.match(r"^python[0-9.]*$", name) and args[:1] and args[0].endswith(FM_SCRIPT_ENDS):  # T-1881: python3 plugin/bin/fm
        name, args = "fm", args[1:]
    if name == "sampo":
        name = "fm"
    pos = _positionals(args)
    joined = " ".join(pos[:2])  # not a list: a path or word fragment, not a list
    for verb in _PUBLISH.get(name, []):
        if joined == verb or joined.startswith(verb + " ") or pos[:1] == [verb]:
            return [("publish", f"{name} {verb}")]
    if name in ("vercel", "netlify") and "--prod" in args:
        return [("publish", f"{name} production deploy")]
    if name == "claude" and pos[:2] == ["plugin", "tag"] and "--push" in args:
        return [("publish", "claude plugin tag --push")]
    if name == "fm" and "inbox" in pos and "ticket" in pos and any(a == "--post" or a.startswith("--post=") for a in args):
        return [("publish", "sampo inbox ticket --post")]  # T-1881: posted on the user's ask, not the agent's
    return []
