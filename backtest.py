from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from orb.feed import Mt5Feed
from orb.replay import effective_cost_points, replay, summarize, windows

BASE = Path(__file__).resolve().parent


def apply_overrides(cfg: dict, args) -> dict:
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
    if args.spread is not None:
        cfg.setdefault("costs", {})["spread_points"] = args.spread
    if args.commission is not None:
        cfg.setdefault("costs", {})["commission_per_lot"] = args.commission
    return cfg


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--offset", type=int, default=0,
                        help="skip this many of the most recent days (walk-back)")
    parser.add_argument("--config", default=str(BASE / "config.json"))
    parser.add_argument("--sl-mode", choices=["zone_opposite", "retest_swing"])
    parser.add_argument("--entry-mode", choices=["limit", "retest_close"])
    parser.add_argument("--tp-r", type=float)
    parser.add_argument("--retest-buffer", type=float)
    parser.add_argument("--retest-timeout-bars", type=int)
    parser.add_argument("--min-zone", type=float)
    parser.add_argument("--min-risk", type=float)
    parser.add_argument("--spread", type=float, help="round-trip spread in index points")
    parser.add_argument("--commission", type=float, help="round-trip commission per lot")
    parser.add_argument("--no-trend", action="store_true")


def load_history(cfg: dict, days: int):
    feed = Mt5Feed(cfg["symbol"], int(cfg.get("server_offset_sec_fallback", 10800)))
    feed.start()
    h1 = feed.bars(60, int(cfg["trend"].get("ema_slow", 200)) * 2 + days * 24)
    m15 = feed.bars(15, days * 100 + 400)
    m5 = feed.bars(int(cfg.get("entry_timeframe_minutes", 5)), days * 320 + 400)
    feed.shutdown()
    if not h1 or not m15 or not m5:
        raise SystemExit("not enough history in the terminal - open charts / scroll history first")
    return m15, m5, h1


def print_signals(signals):
    if not signals:
        return
    print(f"{'date':<11} {'dir':<6} {'entry':>9} {'sl':>9} {'risk':>6} {'tp':>9} "
          f"{'trend':<8} {'outcome':<8} {'R':>6}")
    print("-" * 80)
    for s in signals:
        print(
            f"{s.day.isoformat():<11} {s.direction.value:<6} {s.entry:>9.2f} "
            f"{s.sl:>9.2f} {s.risk:>6.1f} {s.tp:>9.2f} {s.trend:<8} {s.outcome:<8} {s.realized_r:>+6.2f}"
        )


def print_summary(st: dict, cost: float) -> None:
    print("-" * 80)
    print(f"days replayed      : {st['days']} (no opening range: {st['skipped']})")
    print(f"signals            : {st['signals']}  "
          f"(TP {st['tp']} / SL {st['sl']} / EOD {st['eod']} / other {st['other']})")
    if st["resolved"]:
        print(f"gross              : {st['win_rate']:.1f}% WR  |  {st['avg_r']:+.2f}R avg  |  {st['total_r']:+.1f}R total")
        print(f"net (cost {cost:.1f} pt) : {st['net_win_rate']:.1f}% WR  |  {st['net_avg_r']:+.2f}R avg  |  {st['net_r']:+.1f}R total")
    risks = st["risks"]
    if risks:
        n = len(risks)
        print(f"stop size (pts)    : min {risks[0]:.1f} | p25 {risks[n//4]:.1f} | "
              f"median {risks[n//2]:.1f} | p75 {risks[3*n//4]:.1f} | max {risks[-1]:.1f}")
    print(f"signals per day    : {st['signals_per_day']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay ORB strategy over MT5 history")
    add_common_args(parser)
    parser.add_argument("--windows", type=int, default=0,
                        help="split results into N chunks to check stability")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg = apply_overrides(cfg, args)
    tz = ZoneInfo(cfg["timezone"])
    cost = effective_cost_points(cfg)

    need = args.days + args.offset
    m15, m5, h1 = load_history(cfg, need)

    signals, day_list, skipped = replay(cfg, m15, m5, h1, tz, days=args.days, offset=args.offset)
    st = summarize(signals, cost, len(day_list), skipped)

    sl_desc = "opposite zone side" if cfg["sl_mode"] == "zone_opposite" else "retest swing"
    trend_desc = f"{cfg['trend']['ema_fast']}/{cfg['trend']['ema_slow']} EMA" + \
        ("" if cfg["trend"].get("enabled") else " OFF")
    print(f"\nReplaying {st['days']} days on {cfg['symbol']} "
          f"({trend_desc}, entry {cfg['entry_mode']}, TP {cfg['tp_r']}R, SL = {sl_desc})\n")
    print_signals(signals)
    print_summary(st, cost)

    if args.windows > 1:
        print(f"\nStability across {args.windows} chunks:")
        print(f"{'chunk':<7} {'n':>4} {'WR':>7} {'net avg':>9} {'net R':>8}")
        for i, chunk in enumerate(windows(signals, args.windows), 1):
            cw = summarize(chunk, cost, 1, 0)
            print(f"{i:<7} {cw['signals']:>4} {cw['net_win_rate']:>6.1f}% "
                  f"{cw['net_avg_r']:>+8.2f}R {cw['net_r']:>+7.1f}R")


if __name__ == "__main__":
    main()
