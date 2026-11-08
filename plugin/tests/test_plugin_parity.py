"""T-1822: every fm code app command has a route in Claude Code, through the plugin or a built-in (memory:
cc-plugin-parity), or is named app-only with a reason, so CI fails when an app command lands without one."""
import argparse
import os
import unittest

import helpers  # noqa: F401 — puts plugin/lib on the path
from sampo import cli
from sampo import smithtui

PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__))))
CC_BUILTINS = {"/model", "ctrl+o", "Esc"}  # Claude Code's own: the model picker, the transcript, stop the turn
# each fm code command's route in Claude Code; a new command needs one here or an APP_ONLY reason
PLUGIN_ROUTES = {"new": "/sampo:intake", "stop": "Esc", "resume": "/sampo:resume", "diff": "git diff",
                 "history": "fm digest", "projects": "fm projects", "model": "/model", "settings": "/sampo:settings",
                 "looks": "/sampo:looks", "backlog": "/sampo:backlog", "hush": "fm settings set motion off",
                 "queue": "/sampo:next",
                 "tickets": "/sampo:capture", "undo": "fm task revert", "pr": "fm pr", "thoughts": "ctrl+o"}
APP_ONLY = {"photo": "the app's own frame; Claude Code draws none to shoot", "breakout": "a game in the app while checks run",
            **dict.fromkeys(("focus", "up", "down", "expand", "follow", "search", "help", "quit"),
                            "moving around the app's own panels")}


def fm_resolves(words, parser=None):
    """Each word down fm's subcommands exists; the first word that isn't a subcommand level ends the walk."""
    parser = parser or cli.build_parser()
    sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if not sub or not words:
        return True
    return words[0] in sub.choices and fm_resolves(words[1:], sub.choices[words[0]])


def resolves(route):
    first = route.split()[0]
    if first == "fm":
        return fm_resolves(route.split()[1:])
    if first.startswith("/sampo:"):
        name = first.split(":", 1)[1]
        return (os.path.isfile(os.path.join(PLUGIN, "skills", name, "SKILL.md"))
                or os.path.isfile(os.path.join(PLUGIN, "commands", f"{name}.md")))
    return first == "git" or route in CC_BUILTINS


class PluginParity(unittest.TestCase):
    def test_every_app_command_has_a_claude_code_route_or_a_reason(self):
        commands = {cmd for cmd, _ in smithtui.COMMAND_LABELS}
        for cmd in sorted(commands):
            with self.subTest(cmd):
                route, why = PLUGIN_ROUTES.get(cmd), APP_ONLY.get(cmd)
                self.assertTrue(bool(route) != bool(why), f"fm code's {cmd}: give it one Claude Code route "
                                f"(PLUGIN_ROUTES) or an app-only reason (APP_ONLY)")
                if route:
                    self.assertTrue(resolves(route), f"{cmd} → {route}: no such fm verb, skill, command or built-in")
        self.assertEqual(set(PLUGIN_ROUTES) | set(APP_ONLY), commands, "a route for no command")

    def test_a_route_that_doesnt_exist_is_caught(self):
        self.assertTrue(resolves("fm pr") and resolves("/sampo:intake") and resolves("/sampo:looks"))
        self.assertFalse(resolves("fm nosuchverb"))
        self.assertFalse(resolves("fm task nosuchaction"))
        self.assertFalse(resolves("/sampo:nosuchskill"))
        self.assertFalse(resolves("/nosuch"))

    def test_fm_ships_in_the_plugin(self):
        self.assertTrue(os.access(os.path.join(PLUGIN, "bin", "fm"), os.X_OK))


if __name__ == "__main__":
    unittest.main()
