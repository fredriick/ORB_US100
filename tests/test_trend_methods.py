from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from orb.models import Bar
from orb.trend_methods import build, ema, trend_label

NY = ZoneInfo("America/New_York")


def make_h1(closes, start=None):
    start = start or datetime(2026, 5, 1, 13, 30, tzinfo=timezone.utc)
    bars = []
    t = start
    for c in closes:
        bars.append(Bar(60, t, c, c + 2, c - 2, c, 100))
        t += timedelta(hours=1)
    return bars


class TestEma(unittest.TestCase):
    def test_ema_requires_period(self):
        self.assertIsNone(ema([1, 2], 5))

    def test_trend_label_direction(self):
        up = list(range(1, 300))
        self.assertEqual(trend_label(up, 50, 200), "up")
        self.assertEqual(trend_label(list(reversed(up)), 50, 200), "down")


class TestMethods(unittest.TestCase):
    def test_disabled_is_neutral(self):
        bars = make_h1(range(1, 300))
        fn = build(bars, NY, {"enabled": False, "method": "ema_cross"})
        self.assertEqual(fn(bars[-1].close_time), "neutral")

    def test_unknown_method_raises(self):
        bars = make_h1(range(1, 300))
        with self.assertRaises(ValueError):
            build(bars, NY, {"enabled": True, "method": "nope"})

    def test_ema_cross_up_and_down(self):
        up = make_h1(range(1, 300))
        self.assertEqual(build(up, NY, {"method": "ema_cross"})(up[-1].close_time), "up")
        down = make_h1(range(300, 1, -1))
        self.assertEqual(build(down, NY, {"method": "ema_cross"})(down[-1].close_time), "down")

    def test_price_ema200(self):
        up = make_h1(range(1, 300))
        self.assertEqual(build(up, NY, {"method": "price_ema200"})(up[-1].close_time), "up")

    def test_ema_slope200(self):
        up = make_h1(range(1, 300))
        self.assertEqual(build(up, NY, {"method": "ema_slope200"})(up[-1].close_time), "up")
        down = make_h1(range(300, 1, -1))
        self.assertEqual(build(down, NY, {"method": "ema_slope200"})(down[-1].close_time), "down")

    def test_prior_day_bias(self):
        start = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        bars = make_h1([100] * 28 + [120] * 24, start=start)
        fn = build(bars, NY, {"method": "prior_day"})
        self.assertEqual(fn(bars[-2].close_time), "up")

    def test_adx_returns_valid_label(self):
        bars = make_h1(range(1, 300))
        fn = build(bars, NY, {"method": "adx_gate", "adx_threshold": 20.0})
        self.assertIn(fn(bars[-1].close_time), ("up", "down", "neutral"))


if __name__ == "__main__":
    unittest.main()
