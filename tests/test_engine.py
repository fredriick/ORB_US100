from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from orb.engine import OrbEngine, State
from orb.models import Bar, Direction
from orb.trend import ema, trend_label

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 14)
OPEN = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)  # 09:30 EDT
CUTOFF = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)  # 16:00 EDT


def base_cfg(**over):
    cfg = {
        "session_open_local": "09:30",
        "session_cutoff_local": "16:00",
        "min_zone_range": 0.0,
        "entry_mode": "limit",
        "sl_mode": "zone_opposite",
        "sl_buffer": 1.0,
        "tp_r": 2.0,
        "retest_buffer": 1.0,
        "retest_timeout_bars": 12,
        "allow_rearm": False,
        "max_signals_per_day": 1,
        "trend": {"enabled": True, "timeframe_minutes": 60, "ema_fast": 50, "ema_slow": 200},
    }
    cfg.update(over)
    return cfg


def make_engine(trend="up", cfg=None):
    cfg = cfg or base_cfg()
    return OrbEngine(cfg, lambda t: trend, DAY, NY)


def m5(offset_min: int, o, h, l, c) -> Bar:
    return Bar(5, OPEN + timedelta(minutes=offset_min), o, h, l, c)


def arm_zone(eng: OrbEngine, high=110.0, low=95.0):
    eng.on_orb_bar(Bar(15, OPEN, low, high, low, high - 2))


class TestZone(unittest.TestCase):
    def test_zone_marked(self):
        eng = make_engine()
        arm_zone(eng, 110, 95)
        self.assertEqual(eng.state, State.ARMED)
        self.assertEqual(eng.zone.high, 110)
        self.assertEqual(eng.zone.low, 95)

    def test_min_zone_range_blocks(self):
        eng = make_engine(cfg=base_cfg(min_zone_range=50))
        arm_zone(eng, 110, 95)
        self.assertEqual(eng.state, State.DONE)
        self.assertIsNone(eng.zone)

    def test_duplicate_orb_ignored(self):
        eng = make_engine()
        arm_zone(eng, 110, 95)
        eng.on_orb_bar(Bar(15, OPEN, 0, 1, 0, 1))
        self.assertEqual(eng.zone.high, 110)


class TestBreakout(unittest.TestCase):
    def test_no_breakout_inside_zone(self):
        eng = make_engine()
        arm_zone(eng)
        eng.on_5m_bar(m5(15, 100, 109, 99, 105))
        self.assertEqual(eng.state, State.ARMED)

    def test_long_breakout_waits_retest(self):
        eng = make_engine()
        arm_zone(eng)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))  # close > 110
        self.assertEqual(eng.state, State.AWAIT_RETEST)
        self.assertFalse(eng.signals)

    def test_trend_blocks_long_in_downtrend(self):
        eng = make_engine(trend="down")
        arm_zone(eng)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))
        self.assertEqual(eng.state, State.ARMED)

    def test_trend_blocks_short_in_uptrend(self):
        eng = make_engine(trend="up")
        arm_zone(eng)
        eng.on_5m_bar(m5(15, 100, 101, 90, 92))  # close < 95
        self.assertEqual(eng.state, State.ARMED)

    def test_breakout_bar_itself_cannot_retest(self):
        eng = make_engine()
        arm_zone(eng)
        eng.on_5m_bar(m5(15, 105, 113, 94, 112))  # wick back inside, but this is the breakout bar
        self.assertEqual(eng.state, State.AWAIT_RETEST)
        self.assertFalse(eng.signals)


class TestRetest(unittest.TestCase):
    def test_long_signal_on_limit_retest(self):
        eng = make_engine()
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))
        eng.on_5m_bar(m5(20, 112, 112.5, 109.8, 110.5))  # low touches 110+1
        self.assertEqual(len(eng.signals), 1)
        s = eng.signals[0]
        self.assertEqual(s.direction, Direction.LONG)
        self.assertAlmostEqual(s.entry, 110.0)
        self.assertAlmostEqual(s.sl, 94.0)  # zone.low - 1
        self.assertAlmostEqual(s.tp, 142.0)  # 110 + 2*(110-94)
        self.assertEqual(eng.state, State.SIGNAL)

    def test_short_signal(self):
        eng = make_engine(trend="down")
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 100, 101, 90, 92))
        eng.on_5m_bar(m5(20, 92, 95.5, 91, 94))  # high reaches 95-1
        self.assertEqual(len(eng.signals), 1)
        s = eng.signals[0]
        self.assertEqual(s.direction, Direction.SHORT)
        self.assertAlmostEqual(s.entry, 95.0)
        self.assertAlmostEqual(s.sl, 111.0)  # zone.high + 1
        self.assertAlmostEqual(s.tp, 63.0)  # 95 - 2*(111-95)

    def test_retest_close_entry_mode(self):
        eng = make_engine(cfg=base_cfg(entry_mode="retest_close"))
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))
        eng.on_5m_bar(m5(20, 112, 112.5, 109.5, 110.2))
        s = eng.signals[0]
        self.assertAlmostEqual(s.entry, 110.2)

    def test_timeout_no_retest(self):
        eng = make_engine()
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))
        for i in range(12):
            eng.on_5m_bar(m5(20 + 5 * i, 112, 116, 111.5, 115))
        self.assertEqual(eng.state, State.DONE)
        self.assertFalse(eng.signals)

    def test_collapse_through_zone_fills_and_stops_same_bar(self):
        eng = make_engine(trend="up")
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))  # long breakout
        self.assertEqual(eng.state, State.AWAIT_RETEST)
        # next bar collapses through the boundary AND the whole zone:
        # the limit at 110 fills on the way down, SL at 94 is hit same bar
        eng.on_5m_bar(m5(20, 112, 113, 90, 92))
        self.assertEqual(len(eng.signals), 1)
        self.assertEqual(eng.signals[0].outcome, "SL")
        self.assertEqual(eng.state, State.SIGNAL)

    def test_against_trend_then_aligned_breakout_later(self):
        eng = make_engine(trend="down")
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))  # long breakout vs downtrend
        self.assertEqual(eng.state, State.ARMED)
        # price keeps drifting, later the aligned short breakout appears
        eng.on_5m_bar(m5(20, 112, 113, 105, 106))
        eng.on_5m_bar(m5(25, 106, 107, 90, 92))  # close < zone.low, trend down ok
        self.assertEqual(eng.state, State.AWAIT_RETEST)
        self.assertEqual(eng._pending.direction, Direction.SHORT)

    def test_neutral_trend_allows_both_sides(self):
        eng = make_engine(trend="neutral")
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))
        self.assertEqual(eng.state, State.AWAIT_RETEST)
        self.assertEqual(eng._pending.direction, Direction.LONG)


class TestPaperOutcomes(unittest.TestCase):
    def _signal_long(self, eng):
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))
        eng.on_5m_bar(m5(20, 112, 112.5, 109.8, 110.5))
        return eng.signals[0]

    def test_tp_hit(self):
        eng = make_engine()
        s = self._signal_long(eng)
        eng.on_5m_bar(m5(25, 111, 143, 110, 140))
        self.assertEqual(s.outcome, "TP")

    def test_sl_hit(self):
        eng = make_engine()
        s = self._signal_long(eng)
        eng.on_5m_bar(m5(25, 111, 112, 93, 94))
        self.assertEqual(s.outcome, "SL")

    def test_same_bar_sl_is_conservative(self):
        eng = make_engine()
        s = self._signal_long(eng)  # retest bar: low 109.8, no touch of sl yet
        # next bar spans both sl and tp -> SL wins (conservative)
        eng.on_5m_bar(m5(25, 111, 145, 90, 120))
        self.assertEqual(s.outcome, "SL")

    def test_eod_exit_when_holding_through_cutoff(self):
        eng = make_engine()
        s = self._signal_long(eng)
        eng.on_5m_bar(m5(25, 111, 120, 105, 115))  # no tp/sl, before cutoff -> still open
        self.assertEqual(s.outcome, "OPEN")
        eod_bar = Bar(5, CUTOFF, 115, 118, 112, 114)
        eng.on_5m_bar(eod_bar)
        self.assertEqual(s.outcome, "EOD")
        self.assertAlmostEqual(s.exit_price, 114.0)
        self.assertAlmostEqual(s.realized_r, (114 - 110) / 16)

    def test_unresolved_at_day_end_exits_eod(self):
        eng = make_engine()
        s = self._signal_long(eng)
        eng.on_5m_bar(m5(25, 111, 120, 105, 115))
        self.assertEqual(s.outcome, "OPEN")
        eng.finish_day()
        self.assertEqual(s.outcome, "EOD")
        self.assertAlmostEqual(s.realized_r, (115 - 110) / 16)


class TestSessionRules(unittest.TestCase):
    def test_cutoff_stops_new_setups(self):
        eng = make_engine()
        arm_zone(eng, 110, 95)
        late = Bar(5, CUTOFF, 105, 113, 104, 112)
        eng.on_5m_bar(late)
        self.assertEqual(eng.state, State.DONE)

    def test_signal_tracking_continues_past_cutoff(self):
        eng = make_engine()
        s = self._signal_long_helper(eng)
        late = Bar(5, CUTOFF + timedelta(minutes=5), 111, 145, 110, 140)
        eng.on_5m_bar(late)
        self.assertEqual(s.outcome, "TP")

    def _signal_long_helper(self, eng):
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))
        eng.on_5m_bar(m5(20, 112, 112.5, 109.8, 110.5))
        return eng.signals[0]

    def test_bars_before_open_ignored(self):
        eng = make_engine()
        eng.on_5m_bar(Bar(5, OPEN - timedelta(minutes=5), 1, 2, 1, 2))
        self.assertEqual(eng.state, State.WAITING)

    def test_single_signal_per_day(self):
        eng = make_engine()
        arm_zone(eng, 110, 95)
        eng.on_5m_bar(m5(15, 105, 113, 104, 112))
        eng.on_5m_bar(m5(20, 112, 112.5, 109.8, 110.5))
        eng.on_5m_bar(m5(25, 111, 112, 93, 94))  # SL
        self.assertEqual(len(eng.signals), 1)
        self.assertEqual(eng.signals[0].outcome, "SL")
        eng.on_5m_bar(m5(30, 94, 111, 93, 110.5))  # new breakout while state==SIGNAL
        self.assertEqual(len(eng.signals), 1)


class TestTrendMath(unittest.TestCase):
    def test_ema_constant(self):
        self.assertAlmostEqual(ema([5.0] * 30, 10), 5.0)

    def test_ema_insufficient(self):
        self.assertIsNone(ema([1.0, 2.0], 5))

    def test_trend_label(self):
        up = [float(i) for i in range(300)]
        down = list(reversed(up))
        self.assertEqual(trend_label(up, 50, 200), "up")
        self.assertEqual(trend_label(down, 50, 200), "down")
        self.assertEqual(trend_label(up[:100], 50, 200), "neutral")


if __name__ == "__main__":
    unittest.main()
