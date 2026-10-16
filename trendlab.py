from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest import load_history
from orb.replay import effective_cost_points, replay
from orb.settings import config_path

BASE = Path(__file__).resolve().parent

METHODS = ["off", "ema_cross", "price_ema200", "ema_slope200", "ema_slope50", "adx_gate", "prior_day"]


def net_r(sig, cost):
    return sig.realized_r - (cost / sig.risk if sig.risk else 0.0)


def curve_stats(sigs, cost):
    ordered = sorted(sigs, key=lambda s: s.signal_time)
    eq = peak = 0.0
    dd = 0.0
    for s in ordered:
        eq += net_r(s, cost)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return eq, dd


def bootstrap_ci(values, n=4000, seed=1):
    if len(values) < 2:
        return (float("nan"), float("nan"))
    random.seed(seed)
    means = []
    for _ in range(n):
        means.append(statistics.mean(random.choice(values) for _ in values))
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n)]


def summarize_method(sigs, cost):
    r = [net_r(s, cost) for s in sigs if s.risk]
    if not r:
        return None
    net, dd = curve_stats(sigs, cost)
    ordered = sorted(sigs, key=lambda s: s.signal_time)
    half = len(ordered) // 2
    h1, _ = curve_stats(ordered[:half], cost)
    h2, _ = curve_stats(ordered[half:], cost)
    lo, hi = bootstrap_ci(r)
    wins = sum(1 for x in r if x > 0)
    return {
        "n": len(sigs), "wr": 100.0 * wins / len(r), "mean": statistics.mean(r),
        "net": net, "dd": dd, "ret_dd": net / abs(dd) if dd else float("nan"),
        "h1": h1, "h2": h2, "ci": (lo, hi), "rs": r,
    }


def run_symbol(symbol, base_cfg, days, methods):
    cfg = json.loads(json.dumps(base_cfg))
    cfg["symbol"] = symbol
    tz = ZoneInfo(cfg["timezone"])
    print(f"\n=== {symbol} | {days} days ===")
    try:
        m15, m5, h1 = load_history(cfg, days)
    except SystemExit as exc:
        print(f"  skipped: {exc}")
        return None

    cost = effective_cost_points(cfg)
    results = {}
    for method in methods:
        c = json.loads(json.dumps(cfg))
        c["trend"]["enabled"] = method != "off"
        if method != "off":
            c["trend"]["method"] = method
        sigs, day_list, skipped = replay(c, m15, m5, h1, tz, days=days)
        results[method] = summarize_method(sigs, cost)

    print(f"{'method':<14} {'n':>3} {'WR':>6} {'mean':>7} {'net':>7} {'maxDD':>7} "
          f"{'ret/DD':>7} {'h1':>6} {'h2':>6} {'meanR 95% CI':>20}")
    print("-" * 92)
    off = results.get("off")
    for method in methods:
        st = results[method]
        if st is None:
            print(f"{method:<14} (no signals)")
            continue
        ci = f"[{st['ci'][0]:+.2f}, {st['ci'][1]:+.2f}]"
        flag = ""
        if method != "off" and off is not None:
            flag = "  <-- beats off" if st["net"] > off["net"] else ""
        print(f"{method:<14} {st['n']:>3} {st['wr']:>5.1f}% {st['mean']:>+6.3f}R "
              f"{st['net']:>+6.1f}R {st['dd']:>+6.1f}R {st['ret_dd']:>7.2f} "
              f"{st['h1']:>+5.1f} {st['h2']:>+5.1f} {ci:>20}{flag}")

    if off is not None:
        print(f"\nbaseline (off): {off['n']} trades, {off['net']:+.1f}R net, "
              f"mean {off['mean']:+.3f}R")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Trend-filter robustness lab (no orders, no writes)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--days", type=int, default=150)
    parser.add_argument("--symbol", default=None, help="override symbol (default: config)")
    parser.add_argument("--symbols", default=None,
                        help="comma list of extra symbols to probe (e.g. US500,US30)")
    parser.add_argument("--methods", default=",".join(METHODS))
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    with open(config_path(args.config), encoding="utf-8") as f:
        base_cfg = json.load(f)
    methods = [m for m in args.methods.split(",") if m]
    symbols = [args.symbol or base_cfg["symbol"]]
    if args.symbols:
        symbols += [s for s in args.symbols.split(",") if s]

    for symbol in symbols:
        run_symbol(symbol, base_cfg, args.days, methods)


if __name__ == "__main__":
    main()
