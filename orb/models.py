from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class Bar:
    tf_minutes: int
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(minutes=self.tf_minutes)


@dataclass(frozen=True)
class Zone:
    high: float
    low: float
    open_time: datetime

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class Signal:
    day: date
    direction: Direction
    entry_mode: str
    entry: float
    sl: float
    tp: float
    zone_high: float
    zone_low: float
    trend: str
    breakout_time: datetime
    signal_time: datetime
    outcome: str = "OPEN"
    outcome_time: datetime | None = None
    exit_price: float | None = None

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def planned_r(self) -> float:
        return (self.tp - self.entry) / self.risk if self.risk else 0.0

    @property
    def realized_r(self) -> float:
        if self.exit_price is None or not self.risk:
            return 0.0
        if self.direction == Direction.LONG:
            return (self.exit_price - self.entry) / self.risk
        return (self.entry - self.exit_price) / self.risk
