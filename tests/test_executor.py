from __future__ import annotations

import unittest

from orb.executor import calc_lot

# US100 specs from the live terminal
TICK_VALUE = 0.1
TICK_SIZE = 0.01
VOL_MIN = 0.01
VOL_MAX = 500.0
VOL_STEP = 0.01


class TestCalcLot(unittest.TestCase):
    def test_value_per_point_is_ten_usd(self):
        # 1.0 index point on 1 lot = tick_value / tick_size = 0.1 / 0.01 = 10
        lot = calc_lot(10_000, 1.0, 10.0, TICK_VALUE, TICK_SIZE, VOL_MIN, VOL_MAX, VOL_STEP)
        # 1% of 10000 = 100 USD risk; 10 pt stop -> 100 USD per lot -> 1.0 lot
        self.assertAlmostEqual(lot, 1.0)

    def test_rounds_down_to_step(self):
        # risk 100 USD, 30 pt stop -> per lot 300 -> 0.3333 lot -> 0.33
        lot = calc_lot(10_000, 1.0, 30.0, TICK_VALUE, TICK_SIZE, VOL_MIN, VOL_MAX, VOL_STEP)
        self.assertAlmostEqual(lot, 0.33)

    def test_zero_when_below_min_lot(self):
        # tiny equity cannot fund even 0.01 lot
        lot = calc_lot(85.41, 1.0, 20.0, TICK_VALUE, TICK_SIZE, VOL_MIN, VOL_MAX, VOL_STEP)
        self.assertEqual(lot, 0.0)

    def test_tiny_equity_funds_min_lot_at_higher_risk(self):
        # 2.3% of 85.41 ~= 1.96 USD; 20 pt stop on 0.01 lot = 2.0 USD -> not enough, use more
        lot = calc_lot(85.41, 3.0, 20.0, TICK_VALUE, TICK_SIZE, VOL_MIN, VOL_MAX, VOL_STEP)
        self.assertAlmostEqual(lot, 0.01)

    def test_clamped_to_max(self):
        lot = calc_lot(10_000_000, 1.0, 1.0, TICK_VALUE, TICK_SIZE, VOL_MIN, VOL_MAX, VOL_STEP)
        self.assertAlmostEqual(lot, VOL_MAX)

    def test_guards(self):
        self.assertEqual(calc_lot(10_000, 1.0, 0.0, TICK_VALUE, TICK_SIZE, VOL_MIN, VOL_MAX, VOL_STEP), 0.0)
        self.assertEqual(calc_lot(10_000, 0.0, 10.0, TICK_VALUE, TICK_SIZE, VOL_MIN, VOL_MAX, VOL_STEP), 0.0)
        self.assertEqual(calc_lot(10_000, 1.0, 10.0, 0.0, TICK_SIZE, VOL_MIN, VOL_MAX, VOL_STEP), 0.0)


if __name__ == "__main__":
    unittest.main()
