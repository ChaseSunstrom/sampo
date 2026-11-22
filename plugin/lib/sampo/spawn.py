"""T-5835 (part 1 of T-5768): the spawn table, one place for every path that starts an agent. TABLE says which model each
agent type runs on; `fill` gives a spawn that named no model its type's; `lookup_refusal` says no to a lookup sent to a
general agent (a typed Haiku scout does it cheaper). Small and with no hook payload, so the Claude Code Agent hook, the
headless `claude -p` builders and the cost view import this one module, not the hook's. Its only I/O is one read of
plugin/agents/*.md frontmatter per process (the single source for Sampo's own agents); the table holds the built-ins. SAMPO_SPAWN_TABLE=off
turns both rules off. A model advises, so nothing here gates a close or a merge: the worst a wrong row does is a dearer or
slower spawn."""
import os
import re

from sampo import core as c

# Built-in agent types (Claude Code's own, with no agent file) -> the model they run on. Sampo's own agent types are not
# here: their `model:` frontmatter in plugin/agents/*.md is the one source (agent_models), so a file and this module can't
# drift. Sonnet is the floor for an agent that writes or judges; Haiku is for a lookup that returns file:line hits.
TABLE = {"Explore": "haiku", "claude-code-guide": "haiku", "Plan": "sonnet"}
AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(os.path.dirname(__file__)))), "agents")
_agents = None  # {agent type: model} read from AGENTS_DIR once per process (reset() drops it)
NAMESPACES = c.NAMESPACES  # the plugin's names, the current first: a reader takes every one (never spelled here)
GENERAL = "general-purpose"  # the type of a spawn that names none
SCOUT = "fm-scout"
PROMPT_MAX = 300  # chars: a longer prompt carries context, so it is a task, not a lookup
OFF = ("off", "0", "false", "no")

_OPENER = re.compile(r"^\s*(where|which|find|locate|search for|grep|what (file|files|function|functions|module|modules|line"
                     r"|class|classes)|who (calls|uses|defines|reads|writes))\b", re.I)
# A word anywhere that makes the ask a change or a study: a prompt that finds AND fixes, or finds out why, is no lookup.
_NOT_LOOKUP = re.compile(r"\b(implement\w*|write|fix\w*|refactor\w*|add|create|build|change|edit|rename|remove|delete|update"
                         r"|migrate|patch|design|investigate|explain|why|how|analy[sz]e\w*|review\w*|research|compare|debug"
                         r"|should|plan|summari[sz]e|audit)\b", re.I)


def enabled():
    """False when SAMPO_SPAWN_TABLE is off (off, 0, false, no); on when unset or anything else."""
    return os.environ.get("SAMPO_SPAWN_TABLE", "").strip().lower() not in OFF


def base_type(subagent_type):
    """The type without a sampo: or foreman: namespace, or None when it isn't a non-empty string. Another plugin's
    namespace stays (`some:thing`), so it matches no row."""
    if not isinstance(subagent_type, str):
        return None
    t = subagent_type.strip()
    head, sep, rest = t.partition(":")
    return rest if sep and head in NAMESPACES else t


def _frontmatter(text):
    """(name, model) from the frontmatter block of an agent file's text; None for each the block lacks."""
    lines = text.split("\n")
    name = model = None
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, _, val = line.partition(":")
            val = val.strip().strip("\"'")
            if key == "name" and val:
                name = val
            elif key == "model" and val:
                model = val
    return name, model


def _load(directory):
    """{agent type: model} for each *.md in the folder with a frontmatter `model:` line (its `name:`, else the file's
    stem, is the type); {} when the folder is missing, unreadable or None. A file that can't be read is skipped."""
    found = {}
    try:
        names = sorted(os.listdir(directory)) if directory else []
    except (OSError, TypeError, ValueError):
        return found
    for f in names:
        if not f.endswith(".md"):
            continue
        try:
            with open(os.path.join(directory, f), encoding="utf-8", errors="replace") as fh:
                name, model = _frontmatter(fh.read(4096))
        except (OSError, ValueError):
            continue
        if model:
            found[name or f[:-3]] = model
    return found


def agent_models():
    """Sampo's own agent types and their models, read from plugin/agents/*.md frontmatter on the first call and kept for the
    process: the only I/O here, so a call after the first costs a dict lookup."""
    global _agents
    if _agents is None:
        _agents = _load(AGENTS_DIR)
    return _agents


def reset():
    """Forget the agent files read, so the next call reads them again (a test, or a long process after an update)."""
    global _agents
    _agents = None


def model_for(subagent_type):
    """The model for an agent type (a namespaced name reads as its base): the agent file's `model:` for a Sampo agent, the
    table's for a built-in type; None for a type neither has."""
    t = base_type(subagent_type)
    if not t:
        return None
    return agent_models().get(t) or TABLE.get(t)


def fill(tool_input):
    """tool_input of an Agent call with its model filled from the table when it names none. Returns a new dict, or the
    very input (the same object) when nothing changes: an explicit model stands, a type the table lacks, an untyped spawn,
    a malformed input, or the table off. Never raises and never changes its input."""
    if not isinstance(tool_input, dict) or not enabled():
        return tool_input
    if tool_input.get("model") not in (None, ""):
        return tool_input
    model = model_for(tool_input.get("subagent_type"))
    return {**tool_input, "model": model} if model else tool_input


def lookup_refusal(tool_input):
    """The refusal text for an Agent call that is an untyped or general-purpose spawn whose prompt is a lookup (where is,
    which files, find), naming the typed scout to call instead; None for a typed agent, a build or research prompt, a
    long prompt, a malformed input or the table off. Never raises."""
    if not isinstance(tool_input, dict) or not enabled():
        return None
    t = tool_input.get("subagent_type")
    if t not in (None, "") and base_type(t) != GENERAL:
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or len(prompt) > PROMPT_MAX or not _OPENER.search(prompt) or _NOT_LOOKUP.search(prompt):
        return None
    return (f"That is a lookup, and a general agent runs it on the session's model. Call the scout instead: "
            f"Agent(subagent_type=\"sampo:{SCOUT}\"), or search directly with Grep and Glob. "
            f"If it is a build or a study, say so in the prompt.")
