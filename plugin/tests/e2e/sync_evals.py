#!/usr/bin/env python3
"""Embed rules/foreman.md into every eval case's prompt.md as `append_system_prompt`.

Eval runs use a temporary HOME, so the ~/.claude/rules/foreman.md symlink that `fm install-user` creates isn't
there; carrying the rules in each case makes the referee judge Foreman as installed. test_docs checks they match.
"""
import os
import re

PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = "the rules changed: run python3 plugin/tests/e2e/sync_evals.py"  # T-4051: test_docs' red names its fix


def stale(plugin=PLUGIN):
    """Eval cases whose embedded rules (append_system_prompt) are missing or differ from rules/foreman.md."""
    with open(os.path.join(plugin, "rules", "foreman.md")) as f:
        rules = f.read().strip()
    root, out = os.path.join(plugin, "evals"), []
    for case in sorted(os.listdir(root)):
        path = os.path.join(root, case, "prompt.md")
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            m = re.search(r"^append_system_prompt: \|\n((?:  .*\n|\n)*)", f.read(), re.M)
        if not m or "\n".join(l[2:] for l in m.group(1).rstrip("\n").split("\n")).strip() != rules:
            out.append(case)
    return out


def main():
    with open(os.path.join(PLUGIN, "rules", "foreman.md")) as f:
        rules = f.read().strip()
    block = "append_system_prompt: |\n" + "\n".join(("  " + l) if l else "" for l in rules.split("\n")) + "\n"
    root = os.path.join(PLUGIN, "evals")
    for case in sorted(os.listdir(root)):
        path = os.path.join(root, case, "prompt.md")
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            text = f.read()
        head, sep, body = text[4:].partition("\n---\n")
        head = re.sub(r"^append_system_prompt: \|\n(?:  .*\n|\n)*", "", head + "\n", flags=re.M).rstrip("\n")
        with open(path, "w") as f:
            f.write("---\n" + head + "\n" + block + "---\n" + body)
        print("synced", case)


if __name__ == "__main__":
    main()
