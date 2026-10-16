from __future__ import annotations

import csv
import os

from .models import Signal

FIELDS = [
    "date",
    "direction",
    "entry_mode",
    "entry",
    "sl",
    "tp",
    "zone_high",
    "zone_low",
    "trend",
    "breakout_time",
    "signal_time",
    "outcome",
    "outcome_time",
    "exit_price",
    "realized_r",
]


class TradeLedger:
    """Append-only CSV ledger of signals; outcomes updated in place."""

    def __init__(self, path: str):
        self.path = path
        self.rows: dict[tuple[str, str], dict[str, str]] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row.get("signal_time", ""), row.get("direction", ""))
                if key[0]:
                    self.rows[key] = {k: row.get(k, "") for k in FIELDS}

    @staticmethod
    def _key(sig: Signal) -> tuple[str, str]:
        return (sig.signal_time.isoformat(), sig.direction.value)

    @staticmethod
    def _row(sig: Signal) -> dict[str, str]:
        return {
            "date": sig.day.isoformat(),
            "direction": sig.direction.value,
            "entry_mode": sig.entry_mode,
            "entry": f"{sig.entry:.2f}",
            "sl": f"{sig.sl:.2f}",
            "tp": f"{sig.tp:.2f}",
            "zone_high": f"{sig.zone_high:.2f}",
            "zone_low": f"{sig.zone_low:.2f}",
            "trend": sig.trend,
            "breakout_time": sig.breakout_time.isoformat(),
            "signal_time": sig.signal_time.isoformat(),
            "outcome": sig.outcome,
            "outcome_time": sig.outcome_time.isoformat() if sig.outcome_time else "",
            "exit_price": f"{sig.exit_price:.2f}" if sig.exit_price is not None else "",
            "realized_r": f"{sig.realized_r:+.2f}" if sig.exit_price is not None else "",
        }

    def sync(self, signals: list[Signal]) -> None:
        for sig in signals:
            key = self._key(sig)
            row = self._row(sig)
            if self.rows.get(key) != row:
                self.rows[key] = row
                self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        rows = sorted(self.rows.values(), key=lambda r: r["signal_time"])
        tmp = self.path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, self.path)
        self.dirty = False
