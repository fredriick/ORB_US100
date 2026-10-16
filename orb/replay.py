from __future__ import annotations

from datetime import datetime, timezone

from .engine import OrbEngine
from .models import Bar, Signal
from .trend_methods import build as build_trend_method


def build_trend_fn(h1: list[Bar], cfg: dict, tz=None):
    return build_trend_method(h1, tz, cfg)


def effective_cost_points(cfg: dict) -> float:
    """Round-trip cost in index points: spread + commission converted to points."""
    costs = cfg.get("costs", {})
    spread = float(costs.get("spread_points", 4.0))
    commission = float(costs.get("commission_per_lot", 0.0))
    value_per_point = float(costs.get("value_per_point_per_lot", 10.0))
    commission_points = commission / value_per_point if value_per_point else 0.0
    return spread + commission_points


def replay(
    cfg: dict,
    m15: list[Bar],
    m5: list[Bar],
    h1: list[Bar],
    tz,
    days: int | None = None,
    offset: int = 0,
    start_date=None,
    end_date=None,
) -> tuple[list[Signal], list, int]:
    """Replay the strategy day by day. Returns (signals, days_list, skipped_days)."""
    trend_at = build_trend_fn(h1, cfg["trend"], tz)

    by_day: dict = {}
    for b in m5:
        by_day.setdefault(b.open_time.astimezone(tz).date(), []).append(b)
    orb_by_day: dict = {}
    for b in m15:
        orb_by_day.setdefault(b.open_time.astimezone(tz).date(), []).append(b)

    day_list = sorted(by_day)
    if start_date:
        day_list = [d for d in day_list if d >= start_date]
    if end_date:
        day_list = [d for d in day_list if d <= end_date]
    if offset:
        day_list = day_list[:-offset] if offset < len(day_list) else []
    if days:
        day_list = day_list[-days:]

    open_time = datetime.strptime(cfg["session_open_local"], "%H:%M").time()
    signals: list[Signal] = []
    skipped = 0
    for day in day_list:
        open_utc = datetime.combine(day, open_time, tzinfo=tz).astimezone(timezone.utc)
        engine = OrbEngine(cfg, trend_at, day, tz)

        candidates = [
            b for b in orb_by_day.get(day, [])
            if abs((b.open_time - open_utc).total_seconds()) <= 90
        ]
        if candidates:
            engine.on_orb_bar(min(candidates, key=lambda b: abs((b.open_time - open_utc).total_seconds())))
        if engine.zone is None:
            skipped += 1
            continue

        for b in by_day[day]:
            if b.open_time >= open_utc:
                engine.on_5m_bar(b)
        engine.finish_day()
        signals.extend(engine.signals)

    return signals, day_list, skipped


def net_r(sig: Signal, cost_points: float) -> float:
    return sig.realized_r - (cost_points / sig.risk if sig.risk else 0.0)


def max_drawdown(signals: list[Signal], cost_points: float) -> float:
    """Worst peak-to-trough of the cumulative net-R curve (<= 0)."""
    eq = peak = dd = 0.0
    for s in sorted(signals, key=lambda s: s.signal_time):
        eq += net_r(s, cost_points)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def summarize(signals: list[Signal], cost_points: float, days: int, skipped: int) -> dict:
    sig = signals
    tp = sum(1 for s in sig if s.outcome == "TP")
    sl = sum(1 for s in sig if s.outcome == "SL")
    eod = sum(1 for s in sig if s.outcome == "EOD")
    other = sum(1 for s in sig if s.outcome not in ("TP", "SL", "EOD"))
    resolved = tp + sl + eod

    net_r = [s.realized_r - (cost_points / s.risk if s.risk else 0.0) for s in sig]
    total_r = sum(s.realized_r for s in sig)
    total_net = sum(net_r)
    wins = sum(1 for s in sig if s.realized_r > 0)
    net_wins = sum(1 for r in net_r if r > 0)
    risks = sorted(s.risk for s in sig)

    return {
        "days": days,
        "skipped": skipped,
        "signals": len(sig),
        "tp": tp, "sl": sl, "eod": eod, "other": other,
        "resolved": resolved,
        "total_r": total_r,
        "net_r": total_net,
        "win_rate": 100.0 * wins / resolved if resolved else 0.0,
        "net_win_rate": 100.0 * net_wins / len(net_r) if net_r else 0.0,
        "avg_r": total_r / resolved if resolved else 0.0,
        "net_avg_r": total_net / len(net_r) if net_r else 0.0,
        "cost_points": cost_points,
        "risks": risks,
        "signals_per_day": len(sig) / days if days else 0.0,
    }


def windows(signals: list[Signal], n: int) -> list[list[Signal]]:
    """Split signals into n contiguous date-ordered chunks (stability check)."""
    if n <= 0 or not signals:
        return [signals]
    ordered = sorted(signals, key=lambda s: s.signal_time)
    size = max(1, len(ordered) // n)
    out = [ordered[i:i + size] for i in range(0, len(ordered), size)]
    while len(out) > n:
        out[-2].extend(out[-1])
        out.pop()
    return out
