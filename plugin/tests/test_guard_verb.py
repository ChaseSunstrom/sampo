"""T-3600: the Ward keys refusals on the sampo verb and a few later words (serve, pause off, rule rm, --allow core…).
A word the shell fills in at run time — a variable, a substitution, a brace list or glob, xargs's stdin, an alias, a
computed path to sampo, env -S's string — is read through the line's known values first; one still not literal at or
before the verb, among a gated verb's words or as an --allow value is refused as text the guard can't read."""
import shutil
import subprocess
import unittest
from unittest import mock

from test_guard import GuardCase, g


class BashWords(unittest.TestCase):
    """T-3600 review 4: the guard's builtin and keyword sets are bash's own (compgen -b, compgen -k), frozen; a bash
    that adds one fails here, so the set can't drift."""
    @unittest.skipUnless(shutil.which("bash"), "no bash")
    def test_the_frozen_sets_hold_every_builtin_and_keyword_bash_has(self):
        def words(flag):
            return set(subprocess.run(["bash", "-c", f"compgen -{flag}"], capture_output=True, text=True).stdout.split())
        self.assertLessEqual(words("b"), g.BASH_BUILTINS)
        self.assertLessEqual(words("k"), g.BASH_KEYWORDS)


class ComputedVerb(GuardCase):
    def test_a_verb_the_line_sets_is_read_as_its_value(self):
        self.run_table([
            ("V=serve; sampo $V", "remote"),
            ("V=serve; sampo ${{V}}", "remote"),
            ('V=serve; sampo "$V"', "remote"),
            ("V=serve; fm -p app-1a2b3c $V", "remote"),
            ("V=pause; sampo $V off", "self-authorize"),
            ("V='pause off'; sampo $V", "self-authorize"),  # set once to a literal, split as bash splits it
            # T-4785 round 7: decide gates a later word (--answer), so it is typed here too: a value is no verb-chain word
            ('C="a note"; sampo decide "x" --cites "$C"', "system"),
            ('sampo decide "x" --cites "a note"', None),
            ("V=next; [ -f x ] && V=serve; sampo $V", "system"),  # set twice: unknown
            ("X=off; sampo pause $X", "self-authorize"),
            ("V=next; sampo $V", "system"),  # round 7: the verb is read as typed, never through a value
            ("T=T-1; sampo task show $T", None),
            ('D=~; sampo task evidence T-1 --step 1 --run "rm -rf $D"', "rm-outside"),  # a quoted text stays whole
        ], self.bash)

    def test_a_verb_it_cannot_read_is_refused_and_no_grant_lifts_it(self):
        self.run_table([
            ("sampo $V", "system"),
            ('sampo "$V"', "system"),
            ("fm $V", "system"),
            ("sampo ${{V:-serve}}", "system"),
            ("sampo $(echo serve)", "system"),
            ("sampo `echo serve`", "system"),
            ("sampo {{serve,}}", "system"),
            ("sampo -$X x serve", "system"),  # an option word may take the next as its value
            ("sampo -p $P", "system"),  # P='x serve' splits into the verb
            ("sampo $FLAGS next", "system"),
            ("printf -v V serve; sampo $V", "system"),
            ("read V <<< serve; sampo $V", "system"),
            ("python3 /x/plugin/bin/sampo $V", "system"),
            ("env sampo $V", "system"),
            ("command sampo $V", "system"),
            ("nohup sampo $V", "system"),
            ("/home/alice/.local/bin/sampo $V", "system"),
            ("$HOME/.local/bin/sampo $V", "system"),
            ("eval sampo \\$V", "system"),
            ("eval 'sampo $V'", "system"),
            ("bash -c 'sampo $V'", "system"),
        ], self.bash)
        blocked = self.bash("sampo $V", allow=["system"])
        self.assertBlocked(blocked, "system")
        self.assertIn("no grant lifts this", g.message(blocked, self.ctx()))
        self.assertIn("literally", g.message(blocked, self.ctx()))

    def test_a_gated_word_or_allow_value_it_cannot_read_is_refused(self):
        self.run_table([
            ("sampo pause $X", "system"),
            ("sampo pause {{off,}}", "system"),
            ("sampo rule $X 3", "system"),
            ("sampo plugins $X foo", "system"),
            ("sampo agents $X", "system"),
            ("sampo night $X", "system"),
            ('sampo decide "$X note"', "system"),  # a quoted word may still start as --answer=…
            ("sampo task set T-1 --allow $X", "system"),
            ("sampo task set T-1 --allow=$X", "system"),
            ("sampo task new x --allow $(echo core)", "system"),
            ("sampo serve $X", "remote"),
        ], self.bash)

    def test_a_loop_gives_no_word_of_the_verb_chain(self):
        # T-4785 round 8: the verb chain has no exception: a loop's words are no value credited to it (a loop in a branch
        # that never runs, a pipeline stage, a subshell, after `&` gave the verb a value the shell never ran). A plain
        # top-level loop over literal words is the one reading left: it is rewritten as the commands it runs
        # (_expand_literal_loops, T-0411), the text itself, so the verbs the guard reads are the ones the shell runs.
        self.run_table([
            ("for c in status next; do sampo $c; done", None),  # unrolled: sampo status; sampo next
            ('for c in "status" next; do sampo $c; done', "system"),  # a quoted word is no plain literal: not unrolled
            ('for c in "loop on" "lane max 6"; do echo "== $c"; sampo $c 2>&1 | tail -2; done', "system"),  # not plain
            ('for v in mandate outcomes; do echo "$v: $(sampo $v -h | head -1)"; done', None),  # unrolled
            ('for c in next "serve"; do sampo $c; done', "system"),  # a quoted word is no plain literal: not unrolled
            ('for c in "pause off"; do sampo $c; done', "system"),
            ("for c in serve; do sampo $c; done", "remote"),  # unrolled: sampo serve
            ("for c in pause; do sampo $c off; done", "self-authorize"),
            ("for c in $(cat f); do sampo $c; done", "system"),  # words it can't read
            ("for c in \"next\"; do sampo $c; done; c=$(cat f); sampo $c", "system"),  # set another way too
            ("for c in next; do read c; sampo $c; done", "system"),
            ("(for V in status; do :; done); sampo $V risk off", "system"),
            ("for V in status; do sh -c 'sampo $V risk off'; done", "system"),
            ("if false; then for V in status; do :; done; fi; sampo $V risk off", "system"),
            ("echo x | for V in status; do :; done; sampo $V risk off", "system"),
            ("for V in status; do :; done & sampo $V risk off", "system"),
            ("for V in status; do :; done; sampo $V risk off", "system"),
            ("for V in status; do {{ sampo $V risk off; }}; done", None),  # unrolled
            ("for V in status; do sampo $V risk off | cat; done", None),  # unrolled: sampo status risk off | cat
            # the unroll reads a loop that runs a sampo word only where it is top level in this shell: no `(`, `{`, pipe,
            # `&` or `&&`/`||` branch around it (ANDON answer)
            ("echo hi; for c in status next; do sampo $c; done", None),
            ("(for c in status; do sampo $c; done)", "system"),
            ("(for c in status; do :; done); sampo $c risk off", "system"),
            # behind `&&` / `||` the commands are still the text (a real line: cd DIR && for v in …; do sampo $v …), and V
            # is not kept there
            ("true && for c in status; do sampo $c; done", None),
            ("false || for c in status; do sampo $c; done", None),
            ("true && for c in pause; do sampo $c off; done", "self-authorize"),
            ("true && for c in status; do :; done; sampo $c risk off", "system"),
            ("echo x | for c in status; do sampo $c; done", "system"),
            ("for c in status; do sampo $c; done | cat", "system"),
            ("for c in status; do sampo $c; done & echo x", "system"),
            ("{{ for c in status; do sampo $c; done; }}", "system"),
        ], self.bash)

    def test_a_loop_still_reads_an_argument_word_but_only_inside_its_body(self):
        # the argument positions (an id, a stored command's text) keep the loop's words, for a use between that loop's
        # `do` and its matching `done` with no `(`, `{`, pipe or `&` between
        loop = g._loop_values
        for line, word, want in (
                ("for t in T-1 T-2; do sampo task ac $t add x; done", "$t", ("t", ["T-1", "T-2"])),
                ("for t in T-1; do sampo task ac ${t} add x; done", "${t}", ("t", ["T-1"])),
                ("for t in T-1; do sampo task ac $t add x; done; echo $t", "$t", (None, None)),
                ("for t in T-1; do :; done; sampo task ac $t add x", "$t", (None, None)),
                ("(for t in T-1; do :; done); sampo task ac $t add x", "$t", (None, None)),
                ("for t in T-1; do (sampo task ac $t add x); done", "$t", (None, None)),
                ("for t in T-1; do { sampo task ac $t add x; }; done", "$t", (None, None)),
                ("for t in T-1; do echo $t | cat; done", "$t", (None, None)),
                ("for t in T-1; do sampo task ac $t add x & done", "$t", (None, None)),
                ("for t in T-1; do for u in a; do sampo $t; done; done", "$t", ("t", ["T-1"])),
                ("for t in T-1; do sampo task ac $t add x", "$t", (None, None)),  # no done
                ("for t in $T; do sampo task ac $t add x; done", "$t", (None, None))):
            with self.subTest(line=line):
                with mock.patch.dict(g._DCWD, {"line": line, "lits": {}, "holds": True}):
                    self.assertEqual(loop(word, True), want)

    def test_xargs_fills_words_the_guard_cannot_read(self):
        self.run_table([
            ("echo serve | xargs sampo", "system"),
            ("echo off | xargs sampo pause", "system"),
            ("xargs -I{{}} sampo {{}} < f", "system"),  # -I replaces any word
            ("xargs -a f sampo", "system"),
            ("ls | xargs sampo task show", None),  # the verb is literal and task keys nothing on its words
        ], self.bash)

    def test_an_alias_for_sampo_is_refused(self):
        self.run_table([
            ("alias s=sampo\ns serve", "system"),
            ("alias s='sampo serve'\ns", "system"),
            ("alias s='python3 /x/plugin/bin/sampo'\ns serve", "system"),
            ("shopt -s expand_aliases; alias sampo='sampo '; alias v=serve; sampo v", "system"),
            ('alias s="$X"\ns serve', "system"),
            ("BASH_ALIASES[s]=sampo; s serve", "system"),
            ("alias ll='ls -l'", None),
            ("alias", None),
        ], self.bash)

    def test_a_computed_path_to_sampo_is_refused(self):
        self.run_table([
            ('"$(command -v sampo)" serve', "system"),
            ("$(command -v sampo) serve", "system"),
            ("`which sampo` serve", "system"),
            ("S=$(command -v sampo); $S serve", "system"),
            ("S=sampo; $S serve", "remote"),
            ("$PY script.py", None),  # a line that names no sampo: an ordinary computed name
            ("$X $Y", "system"),  # round 9: read as sampo, its verb is an expansion the guard can't read
            ("$EDITOR notes.txt", None),
            # a quote-blind split leaves a "word" with ; and | in it: that is debris, not a command word
            ('for p in 1 2; do for i in 1 2; do l="$l <- $p:$(ps -o args= -p $p | cut -c1-70)"; p=$pp; '
             '[ "$pp" = 1 ] && break; done; echo "$p $l"; done | cut -c1-520', None),
        ], self.bash)

    def test_env_split_string_runs_its_words(self):
        self.run_table([
            ("env -S 'sampo serve'", "remote"),
            ("env -S'sampo pause off'", "self-authorize"),
            ("env --split-string='sampo serve'", "remote"),
            ("env -iS 'sampo serve'", "remote"),
            ("env -S 'rm -rf ~'", "rm-outside"),
            ("env -S 'sampo next'", None),
        ], self.bash)

    def test_a_wrapper_flag_does_not_swallow_the_program(self):
        self.run_table([
            ("exec -a x sampo serve", "remote"),
            ("exec -c sampo serve", "remote"),
            ("command -p sampo serve", "remote"),
            ("time -p sampo serve", "remote"),
            ("setsid -c sampo serve", "remote"),
            ("exec -a x rm -rf ~", "rm-outside"),
            ("nice -n 10 sampo serve", "remote"),
            ("ionice -c 3 sampo serve", "remote"),
            ("nice -n 10 sampo next", None),
        ], self.bash)

    def test_a_nameref_is_not_read_as_its_literal_target_name(self):
        # review: declare -n r=V makes $r V's value; the line literal r=V would read the ungated word V
        self.run_table([
            ('V=serve; declare -n r=V; sampo "$r"', "system"),
            ('V=serve; local -n r=V; sampo "$r"', "system"),
            ('V=serve; typeset -n r=V; sampo "$r"', "system"),
            ('V=serve; r=V; sampo "${{!r}}"', "system"),
            # review 2: however the setter is spelled, a literal whose value is a name the line names again is no value
            ('V=serve; \\declare -n r=V; sampo "$r"', "system"),
            ('V=serve; d=declare; $d -n r=V; sampo "$r"', "system"),
            ('V=serve; "declare" -n r=V; sampo "$r"', "system"),
            ("V=serve; 'local' -n r=V; sampo \"$r\"", "system"),
            ('V=serve; command declare -n r=V; sampo "$r"', "system"),
            ('r=s; V=serve; declare -n s=V; sampo "${{!r}}"', "system"),
            # review 3: a nameref target set without being written out (REPLY, an array, MAPFILE, an eval-built name):
            # a line that may set a nameref reads no sampo word through the values it sets
            ('read <<< serve; \\declare -n r=REPLY; sampo "$r"', "system"),
            ('A=(serve); \\declare -n r=A[0]; sampo "$r"', "system"),
            ('mapfile <<<serve; \\declare -n r=MAPFILE[0]; sampo "$r"', "system"),
            ('a=S; b=E; eval "$a$b=serve"; \\declare -n r=SE; sampo "$r"', "system"),
            ('read <<< serve; "typeset" -n r=REPLY; sampo "$r"', "system"),
            ('read <<< serve; $D -n r=REPLY; sampo "$r"', "system"),  # an unresolved computed command word
            ('V=next; sampo "$V"', "system"),  # round 7: a verb is typed, not read through a value (was: read as next)
            ('V=x; sampo task new "eval the local models" --ac "y :: test $V"', None),  # prose is no setter
            ('echo "$(read <<< serve; \\declare -n r=REPLY; sampo "$r")"', "system"),  # nor nested in quotes
            # review 4: only code running in this shell rebinds a name (a builtin, a function, a sourced file, a trap,
            # a computed command word); a line holding any but the inert few reads no sampo word through its literals
            ('read <<<serve; for f in a; do f=declare; $f -n r=REPLY; done; sampo "$r"', "system"),
            ('for f in a; do read f <<<declare; $f -n r=REPLY; done; sampo "$r"', "system"),
            ('V=next; . ./f.sh; sampo "$V"', "system"),
            ('V=next; source ./f.sh; sampo "$V"', "system"),
            ("V=next; trap 'V=serve' DEBUG; sampo \"$V\"", "system"),
            ("V=next; mapfile -C 'V=serve; :' -c 1 x < /dev/null; sampo \"$V\"", "system"),
            ('V=next; f() {{ V=serve; }}; f; sampo "$V"', "system"),
            # (round 7: refused either way, the verb is typed; the nameref-reading cases above keep the same verdict)
            ('V=next; f; sampo "$V"', "system"),  # f is a program here: a child can't rebind V
            ('V=next; git status; grep -q x f && echo ok; sampo "$V"', "system"),  # external programs and echo
            ("trap 'echo hi' EXIT; echo x", None),  # these alone still pass
            ("mapfile -C 'echo' -c 1 x < /dev/null", None),
            ("source ./f.sh", None),
            ('V=serve; \\declare -n r=V; for c in $r; do sampo $c; done', "system"),  # nor as a loop's words
        ], self.bash)

    def test_the_refusal_says_what_to_do_for_a_verb_or_a_stored_command(self):
        verb = g.message(self.bash("sampo $V"), self.ctx())
        self.assertIn("type the verb literally", verb)
        stored = g.message(self.bash('sampo task ac T-1 add "x" --verify "pytest $X"'), self.ctx())
        self.assertIn("becomes a command sampo runs and holds $X", stored)
        self.assertIn('pass the id as its own word', stored)
        self.assertNotIn("which sampo verb", stored)
        self.assertIn("no grant lifts this", stored)

    def test_a_word_a_verb_runs_or_stores_is_refused_when_the_shell_fills_it_in(self):
        # review: quiet and at run their trailing words, _fm_stored's verbs and flags keep a command for later
        self.run_table([
            ("read -r C <<< 'sampo serve'; sampo quiet -- \"$C\"", "system"),
            ('sampo quiet "$C"', "system"),
            ('sampo at 10:00 "$C"', "system"),
            ('sampo notify "$C"', "system"),
            ('sampo check add "$T"', "system"),
            ('sampo smoke set cmd "$C"', "system"),
            ('sampo record "$C"', "system"),
            ('sampo whyred "$C"', "system"),
            ('sampo intake "$X"', "system"),
            ('sampo task ac T-1 add "$X"', "system"),
            ('sampo task new x --ac "done :: $C"', "system"),
            ('sampo task new x --verify "$C"', "system"),
            ('sampo task ac T-1 add "$(cat f)"', "system"),  # its output may hold x :: CMD
            ('S=/x; sampo task set T-1 --section "Acceptance criteria" --text "$(cat $S/ac.md)"', "system"),
            ('sampo task set T-1 --section Interpretation --text "$(cat notes.md)"', None),  # no reader takes it
            ('W=/x/plugin/bin/sampo; for f in plugin/bin/sampo $W; do $f status; done', "system"),  # round 8: a loop's words are no program or verb the guard reads
            ('R="bash /x/retry.sh"; id=$($R sampo task new "x")', None),  # the line's literal R, read nested
            ('sed -n "$((l-3)),$((l+1))p" plugin/lib/sampo/cli.py', None),  # a sampo/ folder names no sampo
            ("sampo quiet -- git status", None),
            ('sampo quiet -- git diff "$REF"', None),  # several words are argv: $REF is an argument
            ('sampo task new x --verify "pytest $X"', "system"),
            ("sampo task ac T-1 edit 2 --verify 'for n in a b; do test -f $n; done'", None),  # its own n
            ("sampo check affected 'cargo test -q $(for n in {{names}}; do printf -- \"--test %s \" \"$n\"; done)'", None),
            ('T=$(sampo task new "x"); sampo task ac $T add "y :: true"', None),  # the id stores nothing
            ('sampo task new x --ac "done :: nice -n 10 python3 plugin/tests/run.py test_x"', None),
            ("sampo task set T-1 --section 'Acceptance criteria' --text '- [ ] x: verify with `sampo next`'", None),
            ("sampo notify 'notify-send \"$1\"'", None),  # notify's $1 is its message
        ], self.bash)

    def test_a_computed_command_word_on_a_line_naming_sampo_elsewhere_is_refused(self):
        # review: set by read, an array or a function's "$@", not by a substitution naming sampo
        self.run_table([
            ("read -r C <<< 'sampo serve'; \"$C\"", "system"),
            ('f() {{ "$@"; }}; f sampo serve', "system"),
            ('C=(sampo serve); "${{C[@]}}"', "remote"),  # the ( ) reads as a group that runs sampo serve
            ("source <(echo sampo serve)", "system"),
            ("source <(echo rm -rf ~)", "system"),
            ("echo 'sampo serve' | xargs -I{{}} sh -c '{{}}'", "system"),
            ('T="python3 plugin/tests/run.py test_x"; $T; sampo task evidence T-1 --step 1 --run "$T"', None),
        ], self.bash)

    def test_a_sampo_word_that_is_a_grep_pattern_is_no_call_named_elsewhere(self):
        # T-4922: a grep or rg pattern names text to find; its output reaching the terminal or a filter can't make a
        # computed word on the line sampo. The same word printed alone (-o), written (> f, tee) or fed to a reader or
        # shell stays counted: a later computed word could read it back
        self.run_table([
            ("sed -n '1,50p' run.log | grep -E 'sampo|FAIL'; $CMD", None),
            ("sed -n 1,50p run.log | grep -E sampo | head -3; \"$CMD\" x", None),
            ("grep -i sampo run.log 2>/dev/null; $CMD", None),
            ("rg sampo run.log; $CMD", None),
            ("rg -e sampo -e fm run.log | wc -l; $CMD", None),
            ("grep --regexp=sampo run.log; $CMD", None),
            ("grep -n -- sampo run.log; $CMD", None),
            ("egrep -A 3 sampo run.log; $CMD", None),
            # what the shell fills in still counts: the verb of a variable run as a command, a string read into one
            ("grep -E sampo run.log; V=serve; sampo $V", "remote"),  # serve, read through V
            ("grep sampo run.log; read -r C <<< 'sampo serve'; \"$C\"", "system"),
            ("grep sampo run.log; f() {{ \"$@\"; }}; f sampo serve", "system"),
            ("grep 's''ampo' run.log; read -r C <<< 'sampo serve'; \"$C\"", "system"),  # not the text on the line
            ("grep -o sampo run.log; $CMD", "system"),
            ("grep -oE sampo run.log; $CMD", "system"),
            ("grep --only-matching sampo run.log; $CMD", "system"),
            ("rg -o sampo run.log; $CMD", "system"),
            ("grep sampo run.log > out.txt; $CMD", "system"),
            ("grep sampo run.log | tee out.txt; $CMD", "system"),
            ("grep sampo run.log | sh; $CMD", "system"),
            ("grep sampo run.log | head -1 | {{ read C; $C; }}", "system"),
            ("grep sampo run.log | while read C; do $C; done", "system"),
            ("grep \"$(sampo serve)\" run.log; $CMD", "remote"),  # the substitution is a real call
            ("echo 'sampo serve' | $CMD", "system"),  # echo's text is not a pattern
        ], self.bash)

    def test_ordinary_literal_calls_still_pass(self):
        self.run_table([
            ("sampo next", None),
            ("sampo task show T-1", None),
            ("sampo -p app-1a2b3c next", None),
            ("sampo task show $T", None),
            ('sampo task show "$T"', None),
            ("sampo task evidence T-1 --step 1 --run \"$CMD\"", None),
            ('sampo task log T-1 "steer: $(git log -1 --format=%s)"', None),
            ('sampo decide "costs $5 a run"', None),
            ("for t in T-1 T-2; do sampo task show $t; done", None),
            ("sampo serve status", None),
            ("sampo quiet -- git status", None),
        ], self.bash)
