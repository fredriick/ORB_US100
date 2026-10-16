from __future__ import annotations

import time as _time
from datetime import timezone

from .trend_methods import build as build_trend_method
from .trend_methods import ema, trend_label  # re-exported for callers/tests


class TrendProvider:
    def __init__(self, feed, cfg: dict, tz=None, cache_seconds: float = 30.0):
        self.feed = feed
        self.cfg = cfg
        self.tz = tz or timezone.utc
        self.cache_seconds = cache_seconds
        self._bars = []
        self._last_fetch = 0.0
        self._fn = None

    def trend_at(self, t) -> str:
        if not self.cfg.get("enabled", True):
            return "neutral"
        now = _time.time()
        if now - self._last_fetch > self.cache_seconds or not self._bars:
            self._bars = self.feed.bars(
                int(self.cfg.get("timeframe_minutes", 60)), 600
            )
            self._last_fetch = now
            self._fn = build_trend_method(self._bars, self.tz, self.cfg)
        if self._fn is None:
            return "neutral"
        return self._fn(t)
