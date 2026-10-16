from __future__ import annotations

import time as _time


def ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def trend_label(closes: list[float], fast: int, slow: int) -> str:
    if len(closes) < slow + 5:
        return "neutral"
    f = ema(closes, fast)
    s = ema(closes, slow)
    if f is None or s is None:
        return "neutral"
    if f > s:
        return "up"
    if f < s:
        return "down"
    return "neutral"


class TrendProvider:
    def __init__(self, feed, cfg: dict, cache_seconds: float = 30.0):
        self.feed = feed
        self.cfg = cfg
        self.cache_seconds = cache_seconds
        self._bars = []
        self._last_fetch = 0.0

    def trend_at(self, t) -> str:
        if not self.cfg.get("enabled", True):
            return "neutral"
        now = _time.time()
        if now - self._last_fetch > self.cache_seconds or not self._bars:
            self._bars = self.feed.bars(
                int(self.cfg.get("timeframe_minutes", 60)), 400
            )
            self._last_fetch = now
        closes = [b.close for b in self._bars if b.close_time <= t]
        return trend_label(
            closes,
            int(self.cfg.get("ema_fast", 50)),
            int(self.cfg.get("ema_slow", 200)),
        )
