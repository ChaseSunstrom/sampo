"""Kalevala levels (T-5013, slice of T-3312): one generic level type, no per-depth code. Level k's units are whole
level k-1 setups with their graded sample counts; select picks the best by measured results or says 'not enough
signal' under the floor; budget is geometric; cap is the wall-clock cap; level k ticks once per several level k-1
rounds. All pure: placeholders only, no paths, hosts or clock."""
# map-links: plugin/lib/sampo/kalevala_levels.py
import unittest

import helpers  # noqa: F401  puts plugin/lib on sys.path
from sampo import kalevala_levels as kl


def level(k, **graded):
    return kl.Level(k, tuple(kl.Unit(name, n) for name, n in graded.items()))


class Select(unittest.TestCase):
    def test_best_by_measured_result(self):
        lv = level(2, setup_a=3, setup_b=4, setup_c=5)
        self.assertEqual(kl.select(lv, {"setup_a": 0.4, "setup_b": 0.9, "setup_c": 0.6}, 3), "setup_b")

    def test_same_code_at_every_depth(self):
        for k in (2, 3, 5, 9):
            lv = level(k, x=3, y=3)
            self.assertEqual(kl.select(lv, {"x": 0.1, "y": 0.2}, 3), "y")

    def test_below_floor_is_not_enough_signal(self):
        lv = level(2, setup_a=2, setup_b=2)
        self.assertEqual(kl.select(lv, {"setup_a": 0.1, "setup_b": 0.9}, 3), kl.NOT_ENOUGH_SIGNAL)
        self.assertEqual(kl.NOT_ENOUGH_SIGNAL, "not enough signal")

    def test_floor_is_an_input_and_inclusive(self):
        lv = level(2, a=3, b=3)
        self.assertEqual(kl.select(lv, {"a": 1.0, "b": 0.0}, 3), "a")
        self.assertEqual(kl.select(lv, {"a": 1.0, "b": 0.0}, 4), kl.NOT_ENOUGH_SIGNAL)

    def test_one_unit_over_the_floor_cannot_be_told_apart(self):
        lv = level(2, a=9, b=1)
        self.assertEqual(kl.select(lv, {"a": 1.0, "b": 0.0}, 3), kl.NOT_ENOUGH_SIGNAL)

    def test_under_floor_unit_never_wins_even_with_the_best_score(self):
        lv = level(2, a=5, b=5, thin=1)
        self.assertEqual(kl.select(lv, {"a": 0.5, "b": 0.4, "thin": 1.0}, 3), "a")

    def test_tie_for_best_is_not_enough_signal(self):
        lv = level(2, a=3, b=3)
        self.assertEqual(kl.select(lv, {"a": 0.5, "b": 0.5}, 3), kl.NOT_ENOUGH_SIGNAL)

    def test_missing_or_bad_result_drops_the_unit(self):
        lv = level(2, a=3, b=3, c=3)
        self.assertEqual(kl.select(lv, {"a": 0.2, "b": float("nan")}, 3), kl.NOT_ENOUGH_SIGNAL)
        self.assertEqual(kl.select(lv, {"a": 0.2, "b": float("nan"), "c": 0.7}, 3), "c")

    def test_empty_level_and_bad_floor(self):
        self.assertEqual(kl.select(kl.Level(2, ()), {}, 3), kl.NOT_ENOUGH_SIGNAL)
        with self.assertRaises(ValueError):
            kl.select(level(2, a=3, b=3), {"a": 1, "b": 0}, 0)

    def test_a_unit_cannot_be_named_like_the_verdict(self):
        with self.assertRaises(ValueError):
            kl.Unit(kl.NOT_ENOUGH_SIGNAL, 3)

    def test_duplicate_unit_names_and_negative_counts_refused(self):
        with self.assertRaises(ValueError):
            kl.Level(2, (kl.Unit("a", 3), kl.Unit("a", 4)))
        with self.assertRaises(ValueError):
            kl.Unit("a", -1)


class Budget(unittest.TestCase):
    def test_geometric(self):
        self.assertEqual(kl.budget(2, 100.0, 0.5), 100.0)
        self.assertEqual(kl.budget(3, 100.0, 0.5), 50.0)
        self.assertEqual(kl.budget(5, 100.0, 0.5), 12.5)

    def test_total_over_any_depth_stays_under_twice_level_two(self):
        for frac in (0.1, 0.25, 0.5):
            for depth in (2, 3, 8, kl.MAX_DEPTH):
                self.assertLess(kl.total(depth, 100.0, frac), 2 * kl.budget(2, 100.0, frac))

    def test_frac_over_half_is_refused(self):
        for bad in (0, -0.1, 0.51, 1, 2, float("nan")):
            with self.assertRaises(ValueError):
                kl.budget(3, 100.0, bad)

    def test_bad_inputs(self):
        for k in (0, 1, -2, 2.5, True, kl.MAX_DEPTH + 1):
            with self.assertRaises(ValueError):
                kl.budget(k, 100.0, 0.5)
        for base in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                kl.budget(2, base, 0.5)
        self.assertEqual(kl.budget(2, 0, 0.5), 0)


class CapAndTick(unittest.TestCase):
    def test_cap_grows_with_the_rounds_a_tick_spans(self):
        self.assertEqual(kl.cap(1), kl.CAP_BASE)
        self.assertEqual(kl.cap(2), kl.CAP_BASE * kl.ROUNDS_PER_TICK)
        self.assertEqual(kl.cap(4), kl.CAP_BASE * kl.ROUNDS_PER_TICK ** 3)

    def test_cap_bad_depth(self):
        for k in (0, -1, 1.5, kl.MAX_DEPTH + 1):
            with self.assertRaises(ValueError):
                kl.cap(k)

    def test_level_ticks_once_per_several_rounds_below(self):
        n = kl.ROUNDS_PER_TICK
        self.assertGreater(n, 1)
        due = [r for r in range(0, 4 * n + 1) if kl.due(2, r)]
        self.assertEqual(due, [n, 2 * n, 3 * n, 4 * n])

    def test_level_one_ticks_every_round(self):
        self.assertTrue(all(kl.due(1, r) for r in range(1, 5)))
        self.assertFalse(kl.due(1, 0))

    def test_due_bad_inputs(self):
        with self.assertRaises(ValueError):
            kl.due(0, 3)
        with self.assertRaises(ValueError):
            kl.due(2, -1)


if __name__ == "__main__":
    unittest.main()
