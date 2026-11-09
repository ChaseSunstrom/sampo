"""T-0087: a hook that fails 3 times in a row pauses for 10 minutes (skipped, nothing more logged), visibly: fm ui's view
and fm doctor name it; one success resets it; PreToolUse, the guard, is never paused and keeps failing closed."""
# map-links: plugin/lib/sampo/hooks.py
import json
import os
import unittest

from helpers import ForemanTestCase


class Breaker(ForemanTestCase):
    def log_lines(self):
        path = os.path.join(self.home, "state", "logs", "hooks.log")
        return open(path).read().count("\n") if os.path.exists(path) else 0

    def bad(self, event):
        e = dict(os.environ, FOREMAN_HOME=self.home)
        import subprocess
        import sys
        from helpers import HOOK
        return subprocess.run([sys.executable, HOOK, event], input="{not json", env=e, capture_output=True, text=True,
                              timeout=10, cwd=self.repo)

    def test_three_failures_pause_an_event_visibly_and_a_success_resets_it(self):
        self.fm("init")
        for _ in range(3):
            self.assertEqual(self.bad("Stop").returncode, 0)  # a failing hook never blocks
        logged = self.log_lines()
        self.bad("Stop")
        self.assertEqual(self.log_lines(), logged, "paused: skipped, nothing more logged")
        view = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual(view["health"]["paused_hooks"], ["Stop"])
        self.assertIn("paused: Stop", self.fm("doctor", check=False).stdout)
        state = os.path.join(self.home, "state", "logs", "breaker.json")
        with open(state) as f:
            br = json.load(f)
        br["Stop"]["until"] = 0  # ten minutes on
        with open(state, "w") as f:
            json.dump(br, f)
        self.assertEqual(self.hook("Stop", {}).returncode, 0)  # a success resets it
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["health"]["paused_hooks"], [])

    def test_the_gates_are_never_paused(self):
        self.fm("init")
        for _ in range(4):
            self.assertEqual(self.bad("PreToolUse").returncode, 2)  # fails closed every time
            self.bad("TaskCompleted")  # review: it refuses unevidenced work, so it must keep running
        self.assertNotIn("TaskCompleted", json.loads(self.fm("ui", "--json").stdout)["health"]["paused_hooks"])

    def test_a_damaged_breaker_file_stops_nothing(self):
        self.fm("init")
        path = os.path.join(self.home, "state", "logs", "breaker.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"Stop": "x", "SessionStart": {"until": "soon"}}, f)  # review: valid JSON, wrong shapes
        logged = self.log_lines()
        self.assertEqual(self.hook("Stop", {}).returncode, 0)
        self.assertEqual(self.log_lines(), logged, "no crash in the dispatcher")
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["health"]["paused_hooks"], [])


if __name__ == "__main__":
    unittest.main()
