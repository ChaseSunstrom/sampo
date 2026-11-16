"""T-2564: actionlint is not installed and Sampo installs nothing, so a workflow typo reached CI only after the push. The
lint reads a GitHub workflow file with a small YAML reader (no YAML library is installed) and names each fault with file
and line: a parse error, a missing on/jobs/runs-on/steps, a step with both or neither of run and uses, an unbalanced
expression, a needs naming no job (or a cycle), a matrix/needs/steps reference to nothing, and a run block bash -n rejects.
The two real workflow files must pass."""
# map-links: plugin/lib/sampo/workflowlint.py
import glob
import os
import subprocess
import sys
import tempfile
import time
import unittest

import helpers  # noqa: F401  (puts plugin/lib on the import path)
from sampo import workflowlint as wl

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GOOD = """name: t
on:
  push:
    branches: [main]
  pull_request:
jobs:
  build:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: [ubuntu-latest]
        py: ["3.12", "3.14"]
        include:
          - os: macos-latest
            py: "3.14"
            extra: yes
    steps:
      - uses: actions/checkout@v4
      - id: one
        run: echo "${{ matrix.os }} ${{ matrix.py }} ${{ matrix.extra }}"
      - name: two
        run: |
          if [ -n "${{ steps.one.outputs.x }}" ]; then
            echo yes
          fi
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: echo ${{ needs.build.result }}
"""


def lint(text):
    return wl.lint_text(text)


def at(found, line, word):
    """True when a finding sits on that line and its message holds the word."""
    return any(ln == line and word in msg for ln, msg in found)


class Reader(unittest.TestCase):
    def test_block_flow_quoted_and_block_scalar_values_read_with_their_lines(self):
        doc = wl.load("a: 1\nb:\n  - x\n  - y: 'q'  # c\n    z: [1, 2,\n        3]\nrun: |\n  echo hi\n\n  echo there\nlast: \"a: b\"\n")
        self.assertEqual(doc["a"], "1")
        self.assertEqual(doc["b"][0], "x")
        self.assertEqual(doc["b"][1]["y"], "q")
        self.assertEqual(list(doc["b"][1]["z"]), ["1", "2", "3"])
        self.assertEqual(doc["run"], "echo hi\n\necho there\n")
        self.assertEqual((doc.line_of("a"), doc.line_of("run"), doc["run"].line, doc["last"].line), (1, 7, 8, 11))
        self.assertEqual(doc["last"], "a: b")

    def test_a_sequence_at_the_key_indent_and_an_empty_value_read(self):
        doc = wl.load("k:\n- a\n- b\ne:\nf: 1\n")
        self.assertEqual(list(doc["k"]), ["a", "b"])
        self.assertEqual((doc["e"], doc["f"]), ("", "1"))

    def test_syntax_it_cannot_read_is_an_error_naming_the_line(self):
        for text, line in (("a: *x\n", 1), ("a:\n\tb: 1\n", 2), ("a: 1\na: 2\n", 2), ("a: [1, 2\n", 1),
                           ("a: 'open\n", 1), ("a: 1\n  b: 2\n", 2)):
            with self.assertRaises(wl.YamlError, msg=text) as cm:
                wl.load(text)
            self.assertEqual(cm.exception.line, line, text)


class Faults(unittest.TestCase):
    def test_a_good_workflow_has_no_finding(self):
        self.assertEqual(lint(GOOD), [])

    def test_a_parse_error_is_named_with_its_line(self):
        found = lint("on: push\njobs:\n  a:\n    runs-on: x\n   steps: []\n")
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][0], 5)

    def test_a_missing_on_jobs_runs_on_or_steps_is_named(self):
        self.assertTrue(at(lint("jobs:\n  a:\n    runs-on: x\n    steps:\n      - run: ls\n"), 1, "on"))
        self.assertTrue(at(lint("on: push\nname: t\n"), 1, "jobs"))
        self.assertTrue(at(lint("on: push\njobs:\n  a:\n    steps:\n      - run: ls\n"), 3, "runs-on"))
        self.assertTrue(at(lint("on: push\njobs:\n  a:\n    runs-on: x\n"), 3, "steps"))
        self.assertTrue(at(lint("on: push\njobs:\n  a:\n    runs-on: x\n    steps: []\n"), 5, "steps"))

    def test_a_reusable_workflow_job_needs_no_runs_on_or_steps(self):
        self.assertEqual(lint("on: push\njobs:\n  a:\n    uses: ./.github/workflows/x.yml\n"), [])

    def test_a_step_with_both_or_neither_of_run_and_uses_is_named_at_its_line(self):
        both = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - uses: a/b@v1\n        run: ls\n"
        self.assertTrue(at(lint(both), 6, "both"))
        neither = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - name: only\n      - run: ls\n"
        self.assertTrue(at(lint(neither), 6, "run or uses"))

    def test_a_uses_without_a_ref_is_named_but_a_local_path_is_not(self):
        base = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - uses: %s\n"
        self.assertTrue(at(lint(base % "actions/checkout"), 6, "@"))
        self.assertEqual(lint(base % "./local/action"), [])
        self.assertEqual(lint(base % "docker://alpine:3"), [])

    def test_an_unbalanced_expression_is_named_at_its_line(self):
        opened = "on: push\njobs:\n  a:\n    runs-on: x\n    name: ${{ matrix.x\n    steps:\n      - run: ls\n"
        self.assertTrue(at(lint(opened), 5, "${{"))
        stray = "on: push\njobs:\n  a:\n    runs-on: x\n    name: foo }}\n    steps:\n      - run: ls\n"
        self.assertEqual(lint(stray), [])  # a }} with no ${{ before it is literal text to GitHub
        inrun = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: |\n          ls\n          echo ${{ github.sha\n"
        self.assertTrue(at(lint(inrun), 8, "${{"))

    def test_braces_in_a_run_block_and_a_closing_in_a_string_literal_are_not_unbalanced(self):
        text = ("on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: |\n          awk '{ if (x) { print } }' f\n"
                "          echo ${{ format('{0}}}', github.sha) }}\n")
        self.assertEqual(lint(text), [])

    def test_a_needs_naming_no_job_is_named_and_a_list_is_read(self):
        head = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: ls\n  b:\n    runs-on: x\n"
        self.assertTrue(at(lint(head + "    needs: nope\n    steps:\n      - run: ls\n"), 9, "nope"))
        self.assertTrue(at(lint(head + "    needs: [a, nope]\n    steps:\n      - run: ls\n"), 9, "nope"))
        self.assertEqual(lint(head + "    needs: [a]\n    steps:\n      - run: ls\n"), [])

    def test_a_needs_cycle_is_named(self):
        text = ("on: push\njobs:\n  a:\n    needs: b\n    runs-on: x\n    steps:\n      - run: ls\n"
                "  b:\n    needs: a\n    runs-on: x\n    steps:\n      - run: ls\n")
        self.assertTrue(any("cycle" in m for _, m in lint(text)), lint(text))

    def test_a_matrix_key_nothing_defines_is_named_and_include_keys_count(self):
        bad = GOOD.replace("${{ matrix.py }}", "${{ matrix.pyy }}")
        found = lint(bad)
        self.assertTrue(at(found, 20, "matrix.pyy"), found)
        nomatrix = "on: push\njobs:\n  a:\n    runs-on: ${{ matrix.os }}\n    steps:\n      - run: ls\n"
        self.assertTrue(at(lint(nomatrix), 4, "matrix.os"))

    def test_a_dynamic_matrix_is_not_checked(self):
        text = ("on: push\njobs:\n  a:\n    runs-on: x\n    strategy:\n      matrix: ${{ fromJSON(needs.p.outputs.m) }}\n"
                "    steps:\n      - run: echo ${{ matrix.anything }}\n  p:\n    runs-on: x\n    steps:\n      - run: ls\n")
        self.assertFalse([f for f in lint(text) if "matrix" in f[1]], lint(text))

    def test_a_needs_or_steps_reference_to_nothing_is_named(self):
        bad = GOOD.replace("needs.build.result", "needs.biuld.result")
        self.assertTrue(at(lint(bad), 30, "needs.biuld"), lint(bad))
        bad = GOOD.replace("steps.one.outputs.x", "steps.two.outputs.x")
        self.assertTrue(at(lint(bad), 23, "steps.two"), lint(bad))

    def test_a_run_block_bash_rejects_is_named_at_the_line_inside_the_block(self):
        text = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: |\n          echo ok\n          if [ -z \"$x\" ]; then\n            echo no\n"
        found = lint(text)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][0], 9, found)
        self.assertIn("bash", found[0][1])

    def test_an_expression_in_a_run_block_does_not_break_bash(self):
        text = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: python3 x.py --n ${{ matrix.n || 3 }} \"${{ github.ref }}\"\n"
        self.assertEqual([f for f in lint(text) if "bash" in f[1]], [])

    def test_a_run_in_another_shell_is_not_read_as_bash(self):
        text = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - shell: python\n        run: |\n          if (\n"
        self.assertEqual(lint(text), [])

    def test_a_single_line_run_is_named_at_its_own_line(self):
        text = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: echo 'open\n"
        found = lint(text)
        self.assertTrue(at(found, 6, "bash"), found)


class Files(unittest.TestCase):
    def run_main(self, *paths):
        return subprocess.run([sys.executable, "-I", os.path.join(ROOT, "plugin", "lib", "sampo", "workflowlint.py"), *paths],
                              capture_output=True, text=True, timeout=60)

    def test_the_two_real_workflow_files_pass(self):
        files = sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")))
        self.assertGreaterEqual(len(files), 2, files)
        for f in files:
            self.assertEqual(wl.lint_file(f), [], f)

    def test_the_reader_reads_the_real_test_workflow_whole_and_a_typo_in_a_copy_is_named(self):
        path = os.path.join(ROOT, ".github", "workflows", "test.yml")
        with open(path) as f:
            text = f.read()
        job = wl.load(text)["jobs"]["test"]
        self.assertEqual(len(job["strategy"]["matrix"]["include"]), 6)
        self.assertEqual(list(job["strategy"]["matrix"]["python"]), ["3.12", "3.14"])
        self.assertEqual(len(job["steps"]), 4)
        self.assertIn("--shard ${{ matrix.shard }}", job["steps"][3]["run"])
        found = wl.lint_text(text.replace("--shard ${{ matrix.shard }}", "--shard ${{ matrix.shrad }}"))
        self.assertEqual([m for _, m in found], ["matrix.shrad: no matrix key or include entry defines 'shrad'"])
        found = wl.lint_text(text.replace("${{ matrix.experimental && 6 || 3 }}", "${{ matrix.experimental && 6 || 3 "))
        self.assertTrue(any("never closed" in m for _, m in found), found)
        pages = open(os.path.join(ROOT, ".github", "workflows", "pages.yml")).read()
        self.assertEqual([m for _, m in wl.lint_text(pages.replace("needs: build", "needs: bild"))], ["needs names no job: 'bild'"])
        self.assertEqual([m for _, m in wl.lint_text(pages.replace("steps.deployment", "steps.deploymnt"))],
                         ["steps.deploymnt names no step id in this job"])

    def test_a_faulty_file_exits_1_and_the_report_names_file_and_line(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "bad.yml")
            with open(f, "w") as fh:
                fh.write("on: push\njobs:\n  a:\n    needs: nope\n    runs-on: x\n    steps:\n      - run: ls\n")
            r = self.run_main(f)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f + ":4:", r.stdout)

    def test_a_good_file_exits_0_and_no_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "good.yml")
            with open(f, "w") as fh:
                fh.write(GOOD)
            self.assertEqual(self.run_main(f).returncode, 0)
            self.assertEqual(self.run_main(os.path.join(d, "missing.yml")).returncode, 1)

    def test_a_very_long_line_is_read_in_linear_time(self):
        found = wl.lint_text("on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: echo " + "x" + " " * 30000 + "y\n")
        self.assertEqual(found, [])

    def test_a_byte_order_mark_does_not_hide_the_first_key(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "bom.yml")
            with open(f, "wb") as fh:
                fh.write(b"\xef\xbb\xbf" + GOOD.encode())
            self.assertEqual(wl.lint_file(f), [])
        self.assertEqual(wl.lint_text("﻿" + GOOD), [])

    def test_a_huge_file_is_refused_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "big.yml")
            with open(f, "w") as fh:
                fh.write("# x\n" * 300000)
            found = wl.lint_file(f)
        self.assertEqual(len(found), 1, found)
        self.assertIn("too large", found[0][1])


HEAD = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n"


def steps(*lines, runs_on="x", extra=""):
    """A one-job workflow whose steps are the given lines (each already indented to sit under `steps:`)."""
    return f"on: push\njobs:\n  a:\n    runs-on: {runs_on}\n{extra}    steps:\n" + "".join("      " + ln + "\n" for ln in lines)


class FalsePasses(unittest.TestCase):
    """A file GitHub rejects must not pass: once the lint gates a ship, a false pass defeats it."""

    def test_a_colon_in_a_plain_value_is_a_yaml_error_at_its_line(self):
        for line in ("- name: Run tests: unit", "- run: echo a: b", "- run: echo foo:"):
            found = lint(steps(line))
            self.assertTrue(at(found, 6, "YAML not read"), (line, found))
        for line in ('- name: "Run tests: unit"\n        run: ls', "- run: echo http://x:80/y", "- run: echo a:b", "- run: echo a #: b"):
            self.assertEqual(lint(steps(line)), [], line)

    def test_a_needs_cycle_through_a_second_need_is_named_and_a_diamond_is_not(self):
        job = "    runs-on: x\n    steps:\n      - run: ls\n"
        cyc = "on: push\njobs:\n  a:\n    needs: [b, c]\n" + job + "  b:\n" + job + "  c:\n    needs: a\n" + job
        self.assertTrue(any("cycle" in m for _, m in lint(cyc)), lint(cyc))
        diamond = ("on: push\njobs:\n  a:\n" + job + "  b:\n    needs: a\n" + job + "  c:\n    needs: a\n" + job
                   + "  d:\n    needs: [b, c]\n" + job)
        self.assertEqual(lint(diamond), [])
        selfneed = "on: push\njobs:\n  a:\n    needs: a\n" + job
        self.assertTrue(any("cycle" in m for _, m in lint(selfneed)), lint(selfneed))

    def test_a_nested_dash_reads_as_a_sequence_and_an_indicator_start_is_not_text(self):
        doc = wl.load("k:\n  - - a\n    - b\n  - c\n")
        self.assertEqual([list(x) if isinstance(x, list) else x for x in doc["k"]], [["a", "b"], "c"])
        self.assertTrue(at(lint(steps("- - run: ls")), 6, "mapping"), lint(steps("- - run: ls")))
        for bad in ("@x", "%x", "`x"):
            with self.assertRaises(wl.YamlError, msg=bad) as cm:
                wl.load(f"a: {bad}\n")
            self.assertEqual(cm.exception.line, 1)
        self.assertEqual(wl.load("a: x@y\nb: 'a%'\n")["a"], "x@y")

    def test_a_condition_starting_with_a_bang_is_a_yaml_tag_and_needs_the_expression_braces(self):
        for cond in ("!cancelled()", "!failure()", "!contains(github.ref, 'x')"):
            found = lint(steps(f"- if: {cond}\n        run: ls"))
            self.assertTrue(at(found, 6, "${{"), (cond, found))
        self.assertEqual(lint(steps("- if: ${{ !cancelled() }}\n        run: ls")), [])
        self.assertEqual(lint(steps("- if: failure() || !cancelled()\n        run: ls")), [])

    def test_a_colon_in_a_continuation_line_is_tested_without_its_comment(self):
        self.assertEqual(wl.load("a: x\n  y # note: z\n")["a"], "x y")
        for bad in ("a: x\n  y: z\n", "a: x\n  y:\tz\n", "a: x\n  y:\n"):
            with self.assertRaises(wl.YamlError, msg=bad):
                wl.load(bad)

    def test_only_yaml_escape_letters_are_valid_in_a_double_quoted_string(self):
        for bad in ('a: "\\d"\n', 'a: "C:\\Users"\n', 'a: "x\n  \\q"\n'):
            with self.assertRaises(wl.YamlError, msg=bad):
                wl.load(bad)
        self.assertEqual(wl.load('a: "x\\ty\\\\d\\x41\\u00e9\\0\\e\\_"\n')["a"][:4], "x\ty\\")
        self.assertEqual(wl.load("a: 'C:\\Users'\n")["a"], "C:\\Users")

    def test_a_uses_with_an_empty_ref_or_path_is_named(self):
        for u in ("actions/checkout@", '"@v1"'):
            self.assertTrue(at(lint(steps(f"- uses: {u}")), 6, "uses"), u)
        self.assertEqual(lint(steps("- uses: actions/checkout@v4")), [])


class FalseFindings(unittest.TestCase):
    """A file GitHub accepts must not be named: a false finding blocks a ship."""

    def test_document_markers_are_valid_and_a_second_document_is_not(self):
        self.assertEqual(lint("---\n" + GOOD + "...\n"), [])
        self.assertEqual(lint("# top\n---  # start\n" + GOOD), [])
        self.assertTrue(at(lint(GOOD + "---\non: push\n"), 31, "document"), lint(GOOD + "---\non: push\n"))

    def test_the_default_shell_is_not_bash_on_a_runner_that_may_be_windows(self):
        ps = "- run: if ($x) { 1 }"
        self.assertTrue(at(lint(steps(ps, runs_on="ubuntu-latest")), 6, "bash"))
        self.assertEqual(lint(steps(ps, runs_on="windows-latest")), [])
        self.assertEqual(lint(steps(ps, runs_on="[self-hosted, windows]")), [])
        self.assertEqual(lint(steps(ps, runs_on="${{ inputs.runner }}")), [])
        self.assertEqual(lint(steps(ps, runs_on="self-hosted")), [])
        matrix = "    strategy:\n      matrix:\n        os: [ubuntu-latest, windows-latest]\n"
        self.assertEqual(lint(steps(ps, runs_on="${{ matrix.os }}", extra=matrix)), [])
        linux = "    strategy:\n      matrix:\n        os: [ubuntu-latest, macos-14]\n"
        self.assertTrue(at(lint(steps(ps, runs_on="${{ matrix.os }}", extra=linux)), 9, "bash"))
        self.assertTrue(at(lint(steps("- shell: bash\n        run: if ($x) { 1 }", runs_on="windows-latest")), 7, "bash"))

    def test_a_folded_block_keeps_the_breaks_of_its_more_indented_lines(self):
        text = steps("- run: >", "    if true; then", "      echo hi", "    fi")
        self.assertEqual(lint(text), [])
        doc = wl.load("a: >\n  one\n  two\n\n  three\n    four\n  five\n")
        self.assertEqual(doc["a"], "one two\nthree\n  four\nfive\n")

    def test_a_strategy_or_matrix_given_as_an_expression_is_dynamic(self):
        for extra in ("    strategy: ${{ fromJSON(needs.p.outputs.s) }}\n",
                      "    strategy:\n      matrix: ${{ fromJSON(needs.p.outputs.m) }}\n"):
            text = steps("- run: echo ${{ matrix.anything }}", extra=extra).replace("jobs:\n", "jobs:\n  p:\n    runs-on: x\n    steps:\n      - run: ls\n", 1)
            self.assertEqual([f for f in lint(text) if "matrix" in f[1]], [], extra)

    def test_anchors_aliases_and_merge_keys_are_read(self):
        text = ("on: push\nx-env: &env\n  A: 1\n  B: 2\njobs:\n  a:\n    runs-on: &os ubuntu-latest\n    env:\n      <<: *env\n      B: 3\n"
                "    steps: &steps\n      - run: echo ${{ env.A }}\n  b:\n    runs-on: *os\n    steps: *steps\n")
        doc = wl.load(text)
        self.assertEqual((doc["jobs"]["a"]["env"]["A"], doc["jobs"]["a"]["env"]["B"]), ("1", "3"))
        self.assertEqual(doc["jobs"]["b"]["runs-on"], "ubuntu-latest")
        self.assertEqual(len(doc["jobs"]["b"]["steps"]), 1)
        notes = []
        self.assertEqual(wl.lint_text(text, notes), [])
        self.assertTrue(any("merge" in n and "GitHub" in n for n in notes), notes)
        self.assertEqual(wl.load("a: [&x 1, *x]\n")["a"][1], "1")
        for bad in ("a: *nope\n", "a: &x 1\nb:\n  <<: *x\n"):
            with self.assertRaises(wl.YamlError, msg=bad):
                wl.load(bad)

    def test_a_tag_is_ignored_with_a_note_not_a_finding(self):
        notes = []
        self.assertEqual(wl.lint_text(steps("- run: !!str ls"), notes), [])
        self.assertTrue(any("tag" in n for n in notes), notes)

    def test_a_quoted_scalar_over_several_lines_folds_as_yaml_does(self):
        doc = wl.load("a: \"one\n  two\n\n  three\"\nb: 'x\n  y'  # c\nc: \"esc\\\n  aped\"\nd: 1\n")
        self.assertEqual((doc["a"], doc["b"], doc["c"], doc["d"]), ("one two\nthree", "x y", "escaped", "1"))
        self.assertEqual(lint(steps('- run: "echo a', '    echo b"')), [])
        with self.assertRaises(wl.YamlError) as cm:
            wl.load("a: 'never\nb: 1\n")
        self.assertEqual(cm.exception.line, 1)

    def test_a_block_bash_rejects_only_for_extglob_is_a_note_not_a_finding(self):
        body = ["    case x in +(a|b)) echo;; esac"]
        notes = []
        text = steps("- run: |", "    shopt -s extglob", *body)
        self.assertEqual(wl.lint_text(text, notes), [])
        self.assertTrue(any("extglob" in n for n in notes), notes)
        self.assertTrue(at(lint(steps("- run: |", *body)), 7, "bash"), lint(steps("- run: |", *body)))


class Bounds(unittest.TestCase):
    def test_an_expression_over_lines_keeps_the_line_count_for_bash(self):
        text = steps("- run: |", "    echo ${{ a", "    b }}", "    fi")
        self.assertTrue(at(lint(text), 9, "bash"), lint(text))

    def test_jobs_that_alias_one_big_job_or_one_big_step_list_do_not_multiply_the_work(self):
        one_job = ("on: push\njobs:\n  a: &j\n    runs-on: x\n    steps:\n" + "      - run: ls\n" * 30000
                   + "".join(f"  j{n}: *j\n" for n in range(40000)))
        shared = ("on: push\nx-steps: &s\n" + "  - run: ls\n" * 3000 + "jobs:\n"
                  + "".join(f"  j{n}:\n    runs-on: x\n    steps: *s\n" for n in range(20000)))
        needs = ("on: push\nx-needs: &n\n" + "  - j0\n" * 3000 + "jobs:\n  j0:\n    runs-on: x\n    steps:\n      - run: ls\n"
                 + "".join(f"  j{k}:\n    needs: *n\n    runs-on: x\n    steps:\n      - run: ls\n" for k in range(1, 4000)))
        for name, text, capped in (("one job", one_job, False), ("shared steps", shared, True), ("shared needs", needs, True)):
            self.assertLess(len(text), 1_000_000, name)
            notes = []
            t = time.process_time()
            found = wl.lint_text(text, notes)
            dt = time.process_time() - t
            self.assertLess(dt, 4.0, f"{name}: {dt:.1f} CPU s")
            self.assertLessEqual(len(found) + len(notes), 25, f"{name}: {len(found)} findings, {len(notes)} notes")
            self.assertLess(len(repr(found)) + len(repr(notes)), 20000, name)
            self.assertEqual(any("not checked" in m and "file" in m for _, m in found), capped, (name, found[:3]))

    def test_a_flood_of_findings_is_cut_with_one_line_saying_so(self):
        text = "on: push\njobs:\n" + "".join(f"  j{n}:\n    needs: nope{n}\n    runs-on: x\n    steps:\n      - run: ls\n" for n in range(2000))
        found = wl.lint_text(text)
        self.assertLessEqual(len(found), wl.MAX_FINDINGS + 1)
        self.assertIn("more findings", found[-1][1])

    def test_hostile_one_megabyte_files_finish_in_a_few_cpu_seconds(self):
        mb = 1_000_000
        shapes = {
            "spaces": "on: push\n" + "k" + " " * mb + "\n",
            "keyspaces": "on: push\n" + "k" + " " * mb + "z\n",
            "colons": "on: push\n" + "k" + ":" * mb + "\n",
            "hashes": "on: push\n" + "k" + " #" * (mb // 2) + "\n",
            "open flow": "a: [\n" + "x,\n" * (mb // 3),
            "deep flow": "a: " + "[" * 19000 + "\n" + "[" * 19000 + "\n" * 50,
            "nested dashes": "- " * 19000 + "\n" + ("- " * 19000 + "\n") * 40,
            "open quote": "a: 'x\n" + "y\n" * (mb // 2),
            "open expr": "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: |\n" + "          echo ${{ ${{ ${{\n" * (mb // 28),
            "deep indent": "".join(" " * n + "k:\n" for n in range(0, 1300)),
            "many needs": "on: push\njobs:\n" + "".join(f"  j{n}:\n    needs: j{n - 1}\n    runs-on: x\n    steps:\n      - run: ls\n" for n in range(1, mb // 60)),
            "laughs": "".join(f"l{n}: &l{n} [" + ", ".join([f"*l{n - 1}"] * 9) + "]\n" if n else "l0: &l0 [x]\n" for n in range(12)),
            "comments": "# c\n" * (mb // 4),
        }
        total = 0.0
        for name, text in shapes.items():
            t = time.process_time()
            wl.lint_text(text)
            dt = time.process_time() - t
            total += dt
            self.assertLess(dt, 4.0, f"{name}: {dt:.1f} CPU s")
        self.assertLess(total, 12.0, f"{total:.1f} CPU s")


if __name__ == "__main__":
    unittest.main()
