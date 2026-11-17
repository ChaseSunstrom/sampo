"""T-4811: Ward's read of a long shell script is linear in its length, and a trap's rm -rf of a mktemp dir is allowed.
The fixture is synthetic: a while/case argument parser (many assignments) then thousands of lines of if/case/functions
with command substitutions in their arguments, the shapes of a long CI script, none of its text."""
import time

from test_guard import GuardCase, g


PAD = "the quick brown fox jumps over the lazy dog while the build waits for its cache to warm"
NOTE = (PAD + " ") * 17  # a long comment line, the way a CI script documents a step


def parser(n_opts=40):
    """~280 lines: a while/case over $1, each arm assigning several variables, the way a CI script reads its flags."""
    out = ["#!/bin/bash", "set -euo pipefail", "MODE=run", "KEEP=0", "VERBOSE=0", "OUT=./out", "WORK=", ""]
    out.append('while [ $# -gt 0 ]; do')
    out.append('  case "$1" in')
    for i in range(n_opts):
        out += [f"    --opt{i})",
                f"      OPT{i}=1",
                f'      OPT{i}_VAL="$2"',
                f"      OPT{i}_DIR=build/opt{i}",
                f"      LAST=opt{i}",
                "      shift 2",
                "      ;;"]
    out += ['    --keep) KEEP=1; shift ;;', '    --verbose) VERBOSE=1; shift ;;',
            '    --out) OUT="$2"; shift 2 ;;', '    *) echo "unknown $1" >&2; exit 2 ;;', "  esac", "done", ""]
    return out


def body(start, n_blocks=1):
    """A block: a function, an if/elif/else with assignments and echoes that substitute, a case, a nested if."""
    out = []
    for i in range(start, start + n_blocks):
        out += [f"step_{i}() {{",
                f'  local d="build/{i}" now="$(date +%s)"',
                f'  if [ "$MODE" = run ] && [ -d "$d" ]; then',
                f'    echo "step {i} of $(basename "$d") at $now: {PAD}" >&2',
                f"    # {NOTE}",
                f"    STATE_{i}=ready",
                f"    CNT=$((CNT + {i}))",
                f'  elif [ -n "${{OPT{i % 40}:-}}" ]; then',
                f"    STATE_{i}=opt",
                f'    echo opt $(date) $STATE_{i}',
                "  else",
                f"    STATE_{i}=none",
                "  fi",
                f'  case "$STATE_{i}" in',
                f"    ready) echo ready {i} ;;",
                f'    opt) if [ "$VERBOSE" = 1 ]; then echo "opt {i} $(id -u)"; fi ;;',
                f"    *) echo other {i} ;;",
                "  esac",
                "}",
                f'if [ "$MODE" = run ]; then',
                f"  step_{i}",
                f"  TOTAL=$(printf '%s' \"$STATE_{i}\" | wc -c)",
                f'elif [ "$MODE" = dry ]; then',
                f'  echo "would step_{i} on $(hostname)"',
                "else",
                f"  echo skip {i}",
                "fi",
                ""]
    return out


def script(n_lines):
    lines, i = parser(), 0
    while len(lines) < n_lines:
        lines += body(i)
        i += 1
    return "\n".join(lines[:n_lines]) + "\n"


class ScriptScale(GuardCase):
    def read(self, text):
        """CPU seconds of one whole read of `text` (no timer), and what it found."""
        acc = []
        g._DCWD.clear()  # as a top-level reading starts it: a check another test ran left its line's names (3.4 s, not 1.4)
        t = time.process_time()
        g._check_script(text, self.ctx(), 0, acc)
        return time.process_time() - t, acc

    def best(self, text, n=3):
        return min(self.read(text)[0] for _ in range(n))

    def test_the_synthetic_script_has_the_shapes(self):
        text = script(3566)
        self.assertGreater(len(text.splitlines()), 3500)
        self.assertGreater(len(text), 250_000)
        self.assertIn('case "$1" in', text)
        self.assertIn('echo "step 5 of $(basename "$d") at $now: ', text)

    def test_a_long_script_is_read_inside_the_hard_budget(self):
        """the hard budget is 10 x _BUDGET of CPU for the whole script; a fifth is left for a busy machine"""
        text = script(3566)
        t, acc = self.read(text)
        t = min(t, self.read(text)[0])
        self.assertLess(t, g._BUDGET * 10 * 0.8, f"read took {t:.2f} s; found {acc[:3]}")
        self.assertEqual(acc, [])

    def test_the_read_grows_about_linearly(self):
        small, big = self.best(script(900)), self.best(script(1800))
        self.assertLess(big, 3 * small + 0.05, f"900 lines {small * 1000:.0f} ms, 1800 lines {big * 1000:.0f} ms")

    def test_the_prefix_costs_about_the_sum_of_its_parts(self):
        lines = script(3566).splitlines()
        a, b = "\n".join(lines[:300]) + "\n", "\n".join(lines[300:450]) + "\n"
        both = self.best("\n".join(lines[:450]) + "\n")
        alone = self.best(a) + self.best(b)
        self.assertLess(both, 2 * alone + 0.05, f"together {both * 1000:.0f} ms, alone {alone * 1000:.0f} ms")

    def test_a_substitution_in_an_argument_does_not_make_every_tail_read(self):
        """`echo $(date) $X`, bare or quoted, leaves no command a cd may hide in: its tails need no second reading."""
        for line in ('echo $(date) $R%d', 'echo "$(date) $R%d"', 'echo "x $(id -u)" "$R%d"'):
            text = "".join(line % i + "\n" for i in range(120))
            with self.subTest(line=line):
                t, acc = self.read(text)
                self.assertLess(t, 0.6, f"120 lines took {t * 1000:.0f} ms")
                self.assertEqual(acc, [])

    def test_a_trap_that_removes_a_mktemp_dir_is_allowed(self):
        for make in ("work=$(mktemp -d)", 'work="$(mktemp -d)"', "work=$(mktemp -d -p /tmp)"):
            for trap in ("trap 'rm -rf \"$work\"' EXIT", "trap 'rm -rf \"$work\"; exit' EXIT INT TERM",
                         "trap 'rm -rf \"$work\" || true' EXIT"):
                text = f"{make}\n{trap}\ntouch \"$work/x\"\n"
                with self.subTest(make=make, trap=trap):
                    self.assertEqual(self.read(text)[1], [])
                    self.assertIsNone(self.bash(text))  # the whole command, as Bash receives it
                    self.assertIsNone(self.bash(text.replace("\n", "; ", 1)))

    def test_a_mktemp_dir_made_in_an_earlier_piece_is_still_a_temp_dir(self):
        text = 'work="$(mktemp -d)"\n' + "".join(f'echo "line {i} $(date)"\n' for i in range(60)) + \
               "trap 'rm -rf \"$work\"' EXIT\n"
        self.assertEqual(self.read(text)[1], [])

    def test_a_trap_that_removes_a_home_path_is_still_denied(self):
        for text in ('rm -rf "$HOME/x"\n', 'trap \'rm -rf "$HOME/x"\' EXIT\n',
                     'work=$(mktemp -d)\ntrap \'rm -rf "$HOME/x"\' EXIT\n',
                     'work=$(mktemp -d)\ntrap \'[ -n "$work" ] && rm -rf "$HOME/x"\' EXIT\n'):
            with self.subTest(text=text):
                if "&&" not in text:  # (the script reader drops a finding that names a $ it can't resolve)
                    self.assertTrue([f for f in self.read(text)[1] if f[0] == "rm-outside"], text)
                self.assertBlocked(self.bash(text), "rm-outside")

    def test_a_computed_command_in_a_brace_group_or_case_arm_still_reads_the_substitution(self):
        """T-4811 review: the frame-free pass drops `{` and `${…}`; a computed command word behind a brace or a case arm
        (here `cd`) still makes the `$(cd /; rm -rf tmp)` tail read, as before the change"""
        for text in ("A=c; B=d; {{ ${{A}}${{B}} /tmp/x; }}; echo $(cd /; rm -rf tmp)",  # (self.bash formats: {{ is {)
                     "A=c; B=d; case x in x) ${{A}}${{B}} /tmp/x ;; esac; echo $(cd /; rm -rf tmp)",
                     "A=c; B=d\n{{ ${{A}}${{B}} /tmp/x; }}\necho $(cd /; rm -rf tmp)\n"):
            with self.subTest(text=text):
                self.assertIsNotNone(self.bash(text))

    def test_a_piece_that_builds_a_name_keeps_every_carried_assignment(self):
        """T-4811 review: a name built by read, mapfile, let or (( $n = … )) may be any carried one, so none is dropped;
        and the names that change how bash or git reads a line are carried into every piece"""
        state = ["X=/", "Y=build", "cd /tmp"]
        for piece in ('read "$n" <<< y', "(( $n = 1 ))", 'mapfile -t "$n" < f', 'let "$n=1"'):
            with self.subTest(piece=piece):
                self.assertEqual(g._carried(state, piece), state)
        self.assertEqual(g._carried(state, "echo hi"), ["cd /tmp"])
        for name in ("GLOBIGNORE", "GIT_DIR", "SHELLOPTS"):
            self.assertIn(f"{name}=x", g._carried([f"{name}=x"], "echo hi"))

    def test_a_trap_that_removes_a_name_set_twice_is_not_read_as_the_first_value(self):
        """the body runs later: `w` set again to a home path before the trap fires is a home path, not the temp dir"""
        for text in ('w=$(mktemp -d)\ntrap \'rm -rf "$w"\' EXIT\nw="$HOME/x"\n',
                     # a trap whose body sets a name, a signal may fire it before the rm: w is no known folder then
                     'w=$(mktemp -d)\ntrap \'w="$HOME/x"\' USR1\nrm -rf "$w"\n',
                     'w=$(mktemp -d)\ntrap \'cleanup\' USR1\nrm -rf "$w"\n',
                     'w=$(mktemp -d)\nw="$HOME/x"\ntrap \'rm -rf "$w"\' EXIT\n'):
            with self.subTest(text=text):
                self.assertBlocked(self.bash(text), "rm-outside")
