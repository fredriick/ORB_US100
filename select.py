from __future__ import annotations

import argparse
import itertools
import json
import logging
from pathlib import Path
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from backtest import load_history
from orb.replay import effective_cost_points, max_drawdown, net_r, replay, summarize, windows

BASE = Path(__file__).resolve().parent


def parse_list(value: str, cast):
    return [cast(v) for v in value.split(",") if v.strip() != ""]


def evaluate(cfg, m15, m5, h1, tz, days, folds, cost):
    signals, day_list, skipped = replay(cfg, m15, m5, h1, tz, days=days)
    st = summarize(signals, cost, len(day_list), skipped)
    if not st["signals"]:
        return None
    dd = max_drawdown(signals, cost)
    chunks = [c for c in windows(signals, folds) if c]
    win_net = [sum(net_r(s, cost) for s in c) for c in chunks]
    rs = [net_r(s, cost) for s in signals if s.risk]
    return {
        "n": st["signals"],
        "wr": st["net_win_rate"],
        "mean": st["net_avg_r"],
        "net": st["net_r"],
        "dd": dd,
        "ret_dd": st["net_r"] / abs(dd) if dd else float("nan"),
        "worst": min(win_net),
        "pos_windows": sum(1 for w in win_net if w > 0),
        "folds": len(win_net),
        "win_net": win_net,
        "sd": pstdev(rs) if len(rs) > 1 else 0.0,
    }


def rank_key(row, objective):
    if objective == "net":
        return (row["net"], row["worst"], row["pos_windows"])
    if objective == "ret_dd":
        return (row["ret_dd"], row["net"], row["worst"])
    if objective == "mean":
        return (row["mean"], row["net"], row["worst"])
    # default: robustness first - worst window, then how many windows are positive, then net
    return (row["pos_windows"] == row["folds"], row["worst"], row["net"])


def run_symbol(symbol, base_cfg, args):
    cfg0 = json.loads(json.dumps(base_cfg))
    cfg0["symbol"] = symbol
    tz = ZoneInfo(cfg0["timezone"])
    print(f"\n=== {symbol} | {args.days} days | {args.folds} folds | objective={args.objective} ===")
    try:
        m15, m5, h1 = load_history(cfg0, args.days)
    except SystemExit as exc:
        print(f"  skipped: {exc}")
        return None

    cost = effective_cost_points(cfg0)
    grid = itertools.product(
        parse_list(args.sl_modes, str),
        parse_list(args.entry_modes, str),
        parse_list(args.tp_rs, float),
        parse_list(args.min_risks, float),
        parse_list(args.trend, str),
    )

    rows = []
    for sl_mode, entry_mode, tp_r, min_risk, trend in grid:
        cfg = json.loads(json.dumps(cfg0))
        cfg["sl_mode"] = sl_mode
        cfg["entry_mode"] = entry_mode
        cfg["tp_r"] = tp_r
        cfg["min_risk_points"] = min_risk
        cfg["trend"]["enabled"] = trend == "on"
        if args.spread is not None:
            cfg["costs"]["spread_points"] = args.spread
        res = evaluate(cfg, m15, m5, h1, tz, args.days, args.folds, cost)
        if res is None:
            continue
        rows.append({"sl": sl_mode, "entry": entry_mode, "tp": tp_r, "min_risk": min_risk,
                     "trend": trend, **res})

    if not rows:
        print("  no configs produced signals")
        return None

    rows.sort(key=lambda r: rank_key(r, args.objective), reverse=True)

    print(f"{'sl':<13} {'entry':<12} {'tpR':>4} {'minR':>5} {'tr':<3} "
          f"{'n':>4} {'WR':>7} {'mean':>7} {'net':>7} {'maxDD':>7} {'r/DD':>6} "
          f"{'worst':>7} {'+win':>6}")
    print("-" * 100)
    for r in rows:
        print(f"{r['sl']:<13} {r['entry']:<12} {r['tp']:>4.1f} {r['min_risk']:>5.0f} {r['trend']:<3} "
              f"{r['n']:>4} {r['wr']:>6.1f}% {r['mean']:>+6.3f}R {r['net']:>+6.1f}R "
              f"{r['dd']:>+6.1f}R {r['ret_dd']:>6.2f} {r['worst']:>+6.1f}R "
              f"{r['pos_windows']}/{r['folds']:>1}")

    best = rows[0]
    print(f"\nBest ({args.objective}): {best['sl']} / {best['entry']} / TP {best['tp']}R / "
          f"min_risk {best['min_risk']:.0f} / trend {best['trend']} -> {best['n']} trades, "
          f"{best['wr']:.1f}% WR, {best['net']:+.1f}R net, worst fold {best['worst']:+.1f}R, "
          f"{best['pos_windows']}/{best['folds']} folds positive")
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description="Robust walk-forward config selector")
    parser.add_argument("--config", default=str(BASE / "config.json"))
    parser.add_argument("--days", type=int, default=150)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--symbols", default=None, help="extra symbols, e.g. US30")
    parser.add_argument("--objective", default="worst", choices=["worst", "net", "ret_dd", "mean"])
    parser.add_argument("--sl-modes", default="retest_swing")
    parser.add_argument("--entry-modes", default="retest_close")
    parser.add_argument("--tp-rs", default="1.5,2.0,2.5")
    parser.add_argument("--min-risks", default="20,25,30")
    parser.add_argument("--trend", default="off,on")
    parser.add_argument("--spread", type=float, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    with open(args.config, encoding="utf-8") as f:
        base_cfg = json.load(f)
    symbols = [args.symbol or base_cfg["symbol"]]
    if args.symbols:
        symbols += [s for s in args.symbols.split(",") if s]

    best_overall = None
    for symbol in symbols:
        best = run_symbol(symbol, base_cfg, args)
        if best and (best_overall is None or rank_key(best, args.objective) > rank_key(best_overall, args.objective)):
            best_overall = {**best, "symbol": symbol}

    if best_overall:
        print(f"\n>>> Recommended: {best_overall['symbol']} | {best_overall['entry']} / "
              f"{best_overall['sl']} / TP {best_overall['tp']}R / min_risk "
              f"{best_overall['min_risk']:.0f} / trend {best_overall['trend']}")


if __name__ == "__main__":
    main()
