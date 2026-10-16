from __future__ import annotations

import argparse
import itertools
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest import load_history
from orb.replay import effective_cost_points, replay, summarize
from orb.settings import config_path

BASE = Path(__file__).resolve().parent


def parse_list(value: str, cast):
    return [cast(v) for v in value.split(",") if v.strip() != ""]


def main() -> None:
    parser = argparse.ArgumentParser(description="Parameter sweep / robustness check")
    parser.add_argument("--days", type=int, default=150)
    parser.add_argument("--config", default=None)
    parser.add_argument("--sl-modes", default="retest_swing")
    parser.add_argument("--entry-modes", default="retest_close,limit")
    parser.add_argument("--tp-rs", default="1.5,2.0,2.5")
    parser.add_argument("--min-risks", default="10,20,30")
    parser.add_argument("--trend", default="on,off")
    parser.add_argument("--spread", type=float, default=None)
    parser.add_argument("--sort", default="net", choices=["net", "avg", "wr", "n"])
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    with open(config_path(args.config), encoding="utf-8") as f:
        base_cfg = json.load(f)
    tz = ZoneInfo(base_cfg["timezone"])

    sl_modes = parse_list(args.sl_modes, str)
    entry_modes = parse_list(args.entry_modes, str)
    tp_rs = parse_list(args.tp_rs, float)
    min_risks = parse_list(args.min_risks, float)
    trends = parse_list(args.trend, str)

    print(f"Loading {args.days} days of history ...")
    m15, m5, h1 = load_history(base_cfg, args.days)

    rows = []
    combos = list(itertools.product(sl_modes, entry_modes, tp_rs, min_risks, trends))
    print(f"Running {len(combos)} combinations ...\n")
    for sl_mode, entry_mode, tp_r, min_risk, trend in combos:
        cfg = json.loads(json.dumps(base_cfg))
        cfg["sl_mode"] = sl_mode
        cfg["entry_mode"] = entry_mode
        cfg["tp_r"] = tp_r
        cfg["min_risk_points"] = min_risk
        cfg["trend"]["enabled"] = trend == "on"
        if args.spread is not None:
            cfg["costs"]["spread_points"] = args.spread

        signals, days, skipped = replay(cfg, m15, m5, h1, tz, days=args.days)
        st = summarize(signals, effective_cost_points(cfg), len(days), skipped)
        rows.append({
            "sl": sl_mode, "entry": entry_mode, "tp": tp_r, "min_risk": min_risk,
            "trend": trend, "n": st["signals"], "wr": st["net_win_rate"],
            "avg": st["net_avg_r"], "net": st["net_r"],
        })

    rows.sort(key=lambda r: r[args.sort], reverse=True)

    print(f"{'sl_mode':<13} {'entry':<13} {'tpR':>4} {'minR':>5} {'trend':<5} "
          f"{'n':>4} {'WR':>7} {'net avg':>9} {'net R':>8}")
    print("-" * 82)
    for r in rows:
        print(f"{r['sl']:<13} {r['entry']:<13} {r['tp']:>4.1f} {r['min_risk']:>5.0f} "
              f"{r['trend']:<5} {r['n']:>4} {r['wr']:>6.1f}% {r['avg']:>+8.2f}R {r['net']:>+7.1f}R")

    if rows:
        best = rows[0]
        print(f"\nBest by {args.sort}: {best['sl']} / {best['entry']} / TP {best['tp']}R / "
              f"min_risk {best['min_risk']:.0f} / trend {best['trend']} -> "
              f"{best['wr']:.1f}% WR, {best['net']:+.1f}R net over {best['n']} signals")


if __name__ == "__main__":
    main()
