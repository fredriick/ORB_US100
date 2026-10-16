from __future__ import annotations

import argparse
import json
import logging
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from orb.engine import OrbEngine
from orb.feed import Mt5Feed
from orb.models import Bar
from orb.trend import trend_label

BASE = Path(__file__).resolve().parent


def build_trend_fn(h1: list[Bar], cfg: dict):
    opens = [b.close_time for b in h1]
    closes = [b.close for b in h1]
    fast = int(cfg.get("ema_fast", 50))
    slow = int(cfg.get("ema_slow", 200))

    def trend_at(t: datetime) -> str:
        if not cfg.get("enabled", True):
            return "neutral"
        idx = bisect_right(opens, t)
        return trend_label(closes[:idx], fast, slow)

    return trend_at


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay ORB strategy over MT5 history")
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--config", default=str(BASE / "config.json"))
    parser.add_argument("--sl-mode", choices=["zone_opposite", "retest_swing"])
    parser.add_argument("--entry-mode", choices=["limit", "retest_close"])
    parser.add_argument("--tp-r", type=float)
    parser.add_argument("--retest-buffer", type=float)
    parser.add_argument("--retest-timeout-bars", type=int)
    parser.add_argument("--min-zone", type=float)
    parser.add_argument("--min-risk", type=float)
    parser.add_argument("--cost", type=float, default=4.0,
                        help="round-trip cost in index points (spread+slippage)")
    parser.add_argument("--no-trend", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    if args.sl_mode:
        cfg["sl_mode"] = args.sl_mode
    if args.entry_mode:
        cfg["entry_mode"] = args.entry_mode
    if args.tp_r is not None:
        cfg["tp_r"] = args.tp_r
    if args.retest_buffer is not None:
        cfg["retest_buffer"] = args.retest_buffer
    if args.retest_timeout_bars is not None:
        cfg["retest_timeout_bars"] = args.retest_timeout_bars
    if args.min_zone is not None:
        cfg["min_zone_range"] = args.min_zone
    if args.min_risk is not None:
        cfg["min_risk_points"] = args.min_risk
    if args.no_trend:
        cfg["trend"]["enabled"] = False
    tz = ZoneInfo(cfg["timezone"])
    entry_tf = int(cfg.get("entry_timeframe_minutes", 5))

    feed = Mt5Feed(cfg["symbol"], int(cfg.get("server_offset_sec_fallback", 10800)))
    feed.start()

    h1_needed = int(cfg["trend"].get("ema_slow", 200)) * 2 + args.days * 24
    m5_needed = args.days * 320 + 400
    m15_needed = args.days * 100 + 400

    h1 = feed.bars(60, h1_needed)
    m15 = feed.bars(15, m15_needed)
    m5 = feed.bars(entry_tf, m5_needed)
    feed.shutdown()

    if not h1 or not m15 or not m5:
        raise SystemExit("not enough history in the terminal - open charts / scroll history first")

    trend_at = build_trend_fn(h1, cfg["trend"])

    by_day: dict = {}
    for b in m5:
        by_day.setdefault(b.open_time.astimezone(tz).date(), []).append(b)
    orb_by_day: dict = {}
    for b in m15:
        orb_by_day.setdefault(b.open_time.astimezone(tz).date(), []).append(b)

    days = sorted(by_day)
    cutoff = days[-1]
    start = cutoff - timedelta(days=args.days)
    days = [d for d in days if d >= start]

    sl_desc = "opposite zone side" if cfg["sl_mode"] == "zone_opposite" else "retest swing"
    print(f"\nReplaying {len(days)} days on {cfg['symbol']} "
          f"({cfg['trend']['ema_fast']}/{cfg['trend']['ema_slow']} EMA trend filter"
          f"{'' if cfg['trend'].get('enabled') else ' OFF'}, "
          f"TP {cfg['tp_r']}R, SL = {sl_desc})\n")
    print(f"{'date':<11} {'dir':<6} {'entry':>9} {'sl':>9} {'risk':>6} {'tp':>9} {'trend':<8} {'outcome':<8} {'R':>6}")
    print("-" * 80)

    all_signals = []
    skipped = 0
    for day in days:
        open_utc = datetime.combine(
            day,
            datetime.strptime(cfg["session_open_local"], "%H:%M").time(),
            tzinfo=tz,
        ).astimezone(timezone.utc)

        engine = OrbEngine(cfg, trend_at, day, tz)

        candidates = [
            b
            for b in orb_by_day.get(day, [])
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
        all_signals.extend(engine.signals)
        for s in engine.signals:
            print(
                f"{s.day.isoformat():<11} {s.direction.value:<6} {s.entry:>9.2f} "
                f"{s.sl:>9.2f} {s.risk:>6.1f} {s.tp:>9.2f} {s.trend:<8} {s.outcome:<8} {s.realized_r:>+6.2f}"
            )

    tp = sum(1 for s in all_signals if s.outcome == "TP")
    sl = sum(1 for s in all_signals if s.outcome == "SL")
    eod = sum(1 for s in all_signals if s.outcome == "EOD")
    expired = sum(1 for s in all_signals if s.outcome not in ("TP", "SL", "EOD"))
    cost = args.cost
    net_r = [s.realized_r - (cost / s.risk if s.risk else 0.0) for s in all_signals if s.risk]
    total_r = sum(s.realized_r for s in all_signals)
    total_net_r = sum(net_r)
    wins = sum(1 for s in all_signals if s.realized_r > 0)
    net_wins = sum(1 for r in net_r if r > 0)
    resolved = tp + sl + eod
    risks = sorted(s.risk for s in all_signals)
    print("-" * 80)
    print(f"days replayed      : {len(days)} (no opening range: {skipped})")
    print(f"signals            : {len(all_signals)}  (TP {tp} / SL {sl} / EOD {eod} / other {expired})")
    if resolved:
        print(f"win rate           : {100.0 * wins / resolved:.1f}%  ({wins}/{resolved})")
        print(f"avg R per signal   : {total_r / resolved:+.2f}R")
        print(f"total R (gross)    : {total_r:+.1f}R")
        print(f"cost per signal    : {cost:.1f} pts charged against each entry")
        print(f"total R (net)      : {total_net_r:+.1f}R  | net win rate "
              f"{100.0 * net_wins / len(net_r):.1f}%  | net avg "
              f"{total_net_r / max(len(net_r), 1):+.2f}R")
    if risks:
        n = len(risks)
        print(f"stop size (pts)    : min {risks[0]:.1f} | p25 {risks[n//4]:.1f} | "
              f"median {risks[n//2]:.1f} | p75 {risks[3*n//4]:.1f} | max {risks[-1]:.1f}")
        tiny = sum(1 for r in risks if r < 20)
        print(f"stops under 20 pts : {tiny}/{n}")
    print(f"signals per day    : {len(all_signals)/max(len(days),1):.2f}")


if __name__ == "__main__":
    main()
