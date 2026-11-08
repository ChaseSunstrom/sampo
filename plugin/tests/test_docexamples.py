"""T-3289: every `sampo` and `fm` command example in the docs parses against the real CLI. An unknown verb, an invalid
choice or an unrecognized flag fails; a bare example (a missing required argument) and a placeholder form such as
`[--lens ...]` don't. Parse only: the examples are never run, since most are destructive or need real task ids."""
import contextlib
import io
import os
import re
import shlex

from helpers import PLUGIN, SampoTestCase, read_text

ROOT = os.path.dirname(PLUGIN)
PLACEHOLDER = "placeholder"  # what a doc's <angle>, NAME or ... placeholder becomes: no finding is ever about it
# the examples that are not one command, on purpose: a "file: example" prefix -> why. Empty: every real example parses.
ALLOWED = {
    "MASTER.md: sampo landscape [--if-due]|deps": "the one-line grammar of every verb: `|` lists alternatives, not one "
    "command, so its flags are not one verb's. Each verb has its own examples; the parser tests cover the rest.",
    "MASTER.md: sampo recall --backtest-built": "history: the flag was removed by T-2587 (test_recall pins that it is "
    "gone); the map line names it as what went away.",
}
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE = re.compile(r"`((?:sampo|fm)\b[^`\n]*)`")
FENCED = re.compile(r"^\s*(?:\$ )?((?:sampo|fm)\b.*)$")
CUT = re.compile(r"\s(?:\|\||&&|\||;|>>?)\s")  # a shell pipe, list or redirect: the CLI sees only what's before it
UPPER = re.compile(r"[A-Z]")  # a word with a capital is a placeholder (ID, N, PATH:NAME, shader-NAME), never a flag


def doc_files():
    """The docs that teach the CLI: the front door, the map, the install guide, every skill, command and the rules."""
    names = ["README.md", "MASTER.md", "INSTALL.md"]
    skills = os.path.join(PLUGIN, "skills")
    names += [os.path.join("plugin", "skills", d, "SKILL.md") for d in sorted(os.listdir(skills))
              if os.path.isfile(os.path.join(skills, d, "SKILL.md"))]
    names += [os.path.join("plugin", "commands", f) for f in sorted(os.listdir(os.path.join(PLUGIN, "commands")))
              if f.endswith(".md")]
    return names + [os.path.join("plugin", "rules", "foreman.md")]


def examples(text):
    """(line, command) for each `sampo ...` / `fm ...` in backticks, and each such line inside a code fence."""
    out, fenced = [], False
    for no, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif fenced:
            m = FENCED.match(line)
            if m:
                out.append((no, m.group(1).rstrip()))
        else:
            out += [(no, cmd.strip()) for cmd in INLINE.findall(line)]
    return out


def check(raw):
    """What the real CLI says is wrong with one example: a list of findings, [] when it parses (or only misses a
    required argument, which a bare example is allowed to)."""
    from sampo import cli
    line = CUT.split(raw, 1)[0]
    line = re.sub(r"<[^<>]*>", PLACEHOLDER, line)  # <cmd>, <ui root>, "<why>"
    line = line.replace("…", " ").replace("...", " ")  # an ellipsis: "[--lens ...]", "FILE..."
    line = re.sub(r"[\[\]]", " ", line)  # optional brackets: the options inside are still checked
    try:
        words = shlex.split(line, comments=True)[1:]  # the program's name goes
    except ValueError:
        return ["unbalanced quotes"]
    words = [PLACEHOLDER if (UPPER.search(w) or "|" in w) and not w.startswith("-") else w for w in words]
    # "--rule/--fix/--card" and "--url|--port" are one of several flags: each is checked
    words = [f for w in words for f in (re.split(r"[/|]", w) if w.startswith("-") and re.search(r"[/|]", w) else [w])]
    words, _ = cli._alias(words)  # the renamed verbs (smith, moons ...), as main runs them
    parser = cli.build_parser()
    saved, cli._ARGV = cli._ARGV, list(words)  # what _Parser.error reads for a mistyped flag
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            parser.parse_args(cli._options_first(parser, words))
        return []
    except SystemExit as e:
        if e.code in (0, None):  # -h
            return []
    finally:
        cli._ARGV = saved
    msg = err.getvalue()
    found = []
    left = re.search(r"unrecognized arguments: (.+)", msg)
    if left:
        found += [f"unknown {w}" for w in left.group(1).split(" — did you")[0].split() if PLACEHOLDER not in w]
    bad = re.search(r"invalid choice: '([^']*)'", msg) or re.search(r"unknown '([^']*)' \(", msg)
    if bad and PLACEHOLDER not in bad.group(1).lower():  # a placeholder run through type=str.upper is PLACEHOLDER too
        found.append(f"invalid choice {bad.group(1)}")
    gone = re.search(r"(\S+) is gone \(", msg)
    if gone:
        found.append(f"removed {gone.group(1)}")
    return found


def problems(text, name):
    """(file, line, command, finding) for each example in one doc that the real CLI rejects."""
    return [(name, no, cmd, why) for no, cmd in examples(text) for why in check(cmd)]


class DocExamples(SampoTestCase):  # its own FOREMAN_HOME: a planted unknown word is logged to friction, not the live state
    def test_the_checker_reports_a_made_up_flag_and_a_made_up_verb_in_prose_and_fences(self):
        text = "Run `sampo task done ID --madeup-flag` now.\nAlso `sampo nosuchverb ID`.\n```bash\nsampo focus ID --nope\n```\n"
        found = problems(text, "planted.md")
        self.assertEqual(len(found), 3, found)
        self.assertTrue(any("--madeup-flag" in x[3] for x in found), found)
        self.assertTrue(any("nosuchverb" in x[3] for x in found), found)
        self.assertTrue(any("--nope" in x[3] for x in found), found)

    def test_a_bare_example_and_a_placeholder_form_are_not_reported(self):
        text = ("`sampo task done`\n"
                "`sampo task finish ID --audit \"<how>\" [--lens \"edge: <result>\" …]`\n"
                "`sampo smith \"make calc.add add\"` and `sampo moons rate` and `sampo sym PATH:NAME`\n")
        self.assertEqual(problems(text, "planted.md"), [])

    def test_every_doc_example_parses_against_the_real_cli(self):
        self.maxDiff = None
        found, seen = [], 0
        for name in doc_files():
            text = read_text(os.path.join(ROOT, name), encoding="utf-8")
            seen += len(examples(text))
            found += [x for x in problems(text, name)
                      if not any(f"{x[0]}: {x[2]}".startswith(key) for key in ALLOWED)]
        self.assertGreater(seen, 300, "the examples were not all found")
        self.assertEqual([f"{f}:{n}: `{c[:100]}` -> {w}" for f, n, c, w in found], [])
