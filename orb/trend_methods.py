from __future__ import annotations

from bisect import bisect_right
from datetime import timezone

from .models import Bar


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


def _ema_last(values: list[float], period: int) -> float | None:
    return ema(values, period)


def _adx(highs, lows, closes, period: int = 14):
    """Return (adx, plus_di, minus_di) using Wilder smoothing, or (None,0,0)."""
    n = len(closes)
    if n < period * 2 + 1:
        return None, 0.0, 0.0
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    def wilder(vals):
        acc = sum(vals[:period])
        out = [acc]
        for v in vals[period:]:
            acc = acc - acc / period + v
            out.append(acc)
        return out

    tr_s = wilder(trs)
    plus_s = wilder(plus_dm)
    minus_s = wilder(minus_dm)
    dxs = []
    plus_di = minus_di = 0.0
    for tr, p, m in zip(tr_s, plus_s, minus_s):
        if tr == 0:
            continue
        pdi = 100.0 * p / tr
        mdi = 100.0 * m / tr
        denom = pdi + mdi
        dxs.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
        plus_di, minus_di = pdi, mdi
    if len(dxs) < period:
        return None, plus_di, minus_di
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx, plus_di, minus_di


def build(h1: list[Bar], tz, cfg: dict):
    """Return a trend_at(t) -> 'up'|'down'|'neutral' callable.

    cfg keys: enabled, method, ema_fast, ema_slow, slope_lookback, adx_period,
    adx_threshold.
    """
    times = [b.close_time for b in h1]
    closes = [b.close for b in h1]
    highs = [b.high for b in h1]
    lows = [b.low for b in h1]

    if not cfg.get("enabled", True):
        return lambda t: "neutral"

    method = cfg.get("method", "ema_cross")
    fast = int(cfg.get("ema_fast", 50))
    slow = int(cfg.get("ema_slow", 200))

    if method == "ema_cross":
        def trend_at(t):
            i = bisect_right(times, t)
            return trend_label(closes[:i], fast, slow)
        return trend_at

    if method == "price_ema200":
        def trend_at(t):
            i = bisect_right(times, t)
            if i == 0:
                return "neutral"
            e = _ema_last(closes[:i], slow)
            if e is None:
                return "neutral"
            c = closes[i - 1]
            return "up" if c > e else "down"
        return trend_at

    if method in ("ema_slope200", "ema_slope50"):
        period = slow if method.endswith("200") else fast
        look = int(cfg.get("slope_lookback", 5))

        def trend_at(t):
            i = bisect_right(times, t)
            cur = _ema_last(closes[:i], period)
            prev = _ema_last(closes[:i - look], period) if i - look >= period else None
            if cur is None or prev is None:
                return "neutral"
            if cur > prev:
                return "up"
            if cur < prev:
                return "down"
            return "neutral"
        return trend_at

    if method == "adx_gate":
        period = int(cfg.get("adx_period", 14))
        thr = float(cfg.get("adx_threshold", 20.0))

        def trend_at(t):
            i = bisect_right(times, t)
            adx, pdi, mdi = _adx(highs[:i], lows[:i], closes[:i], period)
            if adx is None or adx < thr:
                return "neutral"
            return "up" if pdi >= mdi else "down"
        return trend_at

    if method == "prior_day":
        dayclose: dict = {}
        for b in h1:
            dayclose[b.close_time.astimezone(tz).date()] = b.close
        order = sorted(dayclose)

        def trend_at(t):
            i = bisect_right(times, t)
            if i == 0:
                return "neutral"
            cur = closes[i - 1]
            d = t.astimezone(tz).date()
            prev = None
            for od in order:
                if od < d:
                    prev = dayclose[od]
                else:
                    break
            if prev is None:
                return "neutral"
            if cur > prev:
                return "up"
            if cur < prev:
                return "down"
            return "neutral"
        return trend_at

    raise ValueError(f"unknown trend method: {method}")


def default_tz():
    return timezone.utc
