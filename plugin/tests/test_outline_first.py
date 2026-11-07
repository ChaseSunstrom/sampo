"""T-3670: the big-file Read refusal is a short note (Claude Code prints it as an error block) and allow_large goes ahead."""
import os
import unittest

from helpers import ForemanTestCase

from sampo import hooks


class OutlineFirst(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def read(self, n_defs, **extra):
        path = os.path.join(self.tmp, f"big{n_defs}.py")
        with open(path, "w") as f:
            for i in range(n_defs):
                f.write(f"def function_number_{i}():\n    return {i}\n\n\n" + "# pad\n" * 3)
            f.write("# x\n" * 600)
        return hooks._outline_first({"session_id": "s1", "tool_input": {"file_path": path, **extra}})

    def test_denial_is_at_most_eight_short_lines(self):
        for n in (3, 200):
            msg = self.read(n)
            self.assertTrue(msg)
            lines = msg.splitlines()
            self.assertLessEqual(len(lines), 8, msg)
            self.assertTrue(all(len(x) <= 160 for x in lines), msg)
            self.assertIn("function_number_0", msg)
        self.assertIn("more", msg)

    def test_allow_large_goes_ahead(self):
        self.assertIsNone(self.read(5, allow_large=True))


if __name__ == "__main__":
    unittest.main()
