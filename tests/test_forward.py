from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path

from forward import LiveSignal, bootstrap_ci, compare, read_ledger
from orb.models import Direction, Signal


def live_row(day, direction, entry, sl, outcome, exit_price, r):
    return {
        "date": day,
        "direction": direction,
        "entry": str(entry),
        "sl": str(sl),
        "tp": str(entry + 2 * (entry - sl)),
        "outcome": outcome,
        "exit_price": "" if exit_price is None else str(exit_price),
        "realized_r": "" if exit_price is None else str(r),
        "signal_time": f"{day}T14:00:00+00:00",
    }


class TestReadLedger(unittest.TestCase):
    def test_reads_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            path.write_text(
                "date,direction,entry,sl,tp,outcome,exit_price,realized_r,signal_time\n"
                "2026-09-16,long,100,90,120,TP,120,2.0,2026-09-16T14:00:00+00:00\n",
                encoding="utf-8",
            )
            rows = read_ledger(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].direction, Direction.LONG)
            self.assertAlmostEqual(rows[0].realized_r, 2.0)

    def test_missing_file(self):
        self.assertEqual(read_ledger(Path("nope-does-not-exist.csv")), [])


class TestBootstrap(unittest.TestCase):
    def test_ci_brackets_mean(self):
        lo, hi = bootstrap_ci([1.0, 1.0, 1.0, 1.0])
        self.assertLessEqual(lo, 1.0)
        self.assertGreaterEqual(hi, 1.0)

    def test_short_input(self):
        lo, hi = bootstrap_ci([1.0])
        self.assertNotEqual(lo, lo)  # nan


class TestCompare(unittest.TestCase):
    def _back(self, day, direction, r):
        sig = Signal(
            day=date.fromisoformat(day),
            direction=Direction(direction),
            entry_mode="retest_close",
            entry=100.0,
            sl=90.0,
            tp=120.0,
            zone_high=110.0,
            zone_low=95.0,
            trend="neutral",
            breakout_time=datetime(2026, 9, 16, tzinfo=timezone.utc),
            signal_time=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc),
        )
        sig.outcome = "TP" if r > 0 else "SL"
        sig.exit_price = 120.0 if r > 0 else 90.0
        return sig

    def test_matched_and_missing(self):
        live = [
            LiveSignal(live_row("2026-09-16", "long", 100, 90, "TP", 120, 2.0)),
        ]
        back = [
            self._back("2026-09-16", "long", 2.0),
            self._back("2026-09-17", "short", -1.0),
        ]
        out = io.StringIO()
        with redirect_stdout(out):
            compare(live, back, cost=4.0)
        text = out.getvalue()
        self.assertIn("matched (same day+direction): 1", text)
        self.assertIn("missing in live   : 1", text)

    def test_no_signals(self):
        out = io.StringIO()
        with redirect_stdout(out):
            compare([], [], cost=4.0)
        self.assertIn("live signals      : 0", out.getvalue())


if __name__ == "__main__":
    unittest.main()
