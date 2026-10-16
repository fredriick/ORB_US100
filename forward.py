from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest import load_history
from orb.models import Direction, Signal
from orb.replay import effective_cost_points, replay
from orb.settings import artifact, config_path

BASE = Path(__file__).resolve().parent


class LiveSignal:
    """A signal recorded by the live bot (one row of trades.csv)."""

    def __init__(self, row: dict):
        self.date = date.fromisoformat(row["date"])
        self.direction = Direction(row["direction"])
        self.entry = float(row["entry"] or 0.0)
        self.sl = float(row["sl"] or 0.0)
        self.tp = float(row["tp"] or 0.0)
        self.outcome = row.get("outcome", "OPEN")
        self.exit_price = float(row["exit_price"]) if row.get("exit_price") else None
        self.realized_r = float(row["realized_r"]) if row.get("realized_r") else 0.0
        self.signal_time = datetime.fromisoformat(row["signal_time"])

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)


def read_ledger(path: Path) -> list[LiveSignal]:
    if not path.exists():
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("date") or not row.get("direction"):
                continue
            try:
                out.append(LiveSignal(row))
            except (ValueError, KeyError) as exc:
                logging.getLogger("orb.forward").warning("skipping malformed row: %s", exc)
    return out


def bootstrap_ci(values, n=4000, seed=1) -> tuple[float, float]:
    if len(values) < 2:
        return (float("nan"), float("nan"))
    random.seed(seed)
    means = sorted(statistics.mean(random.choice(values) for _ in values) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def compare(live: list[LiveSignal], back: list[Signal], cost: float) -> None:
    live_by = {(s.date, s.direction.value): s for s in live}
    back_by = {(s.day, s.direction.value): s for s in back}

    all_keys = sorted(set(live_by) | set(back_by))
    matched, missing_live, extra_live, dir_mismatch = [], [], [], []

    print(f"\n{'date':<11} {'live':<6} {'back':<6} {'live R':>7} {'back R':>7} {'note':<24}")
    print("-" * 78)
    for key in all_keys:
        day, direction = key
        lv, bk = live_by.get(key), back_by.get(key)
        if lv and bk:
            matched.append((lv, bk))
            note = "" if lv.outcome == bk.outcome else f"outcome live={lv.outcome} back={bk.outcome}"
            print(f"{day.isoformat():<11} {direction:<6} {direction:<6} "
                  f"{lv.realized_r:>+7.2f} {bk.realized_r:>+7.2f} {note:<24}")
        elif bk:
            missing_live.append(bk)
            print(f"{day.isoformat():<11} {'-':<6} {direction:<6} "
                  f"{'':>7} {bk.realized_r:>+7.2f} {'MISSING LIVE':<24}")
        else:
            extra_live.append(lv)
            print(f"{day.isoformat():<11} {direction:<6} {'-':<6} "
                  f"{lv.realized_r:>+7.2f} {'':>7} {'EXTRA (not in backtest)':<24}")

    # direction mismatches on the same day
    live_days = {s.date: s for s in live}
    back_days = {s.day: s for s in back}
    for day in sorted(set(live_days) & set(back_days)):
        if live_days[day].direction != back_days[day].direction:
            dir_mismatch.append(day)

    print("-" * 78)
    print(f"live signals      : {len(live)}")
    print(f"backtest signals  : {len(back)}")
    print(f"matched (same day+direction): {len(matched)}")
    print(f"missing in live   : {len(missing_live)}")
    print(f"extra in live     : {len(extra_live)}")
    print(f"direction mismatch: {len(dir_mismatch)}"
          + (f" {[d.isoformat() for d in dir_mismatch]}" if dir_mismatch else ""))

    if matched:
        deltas = [lv.realized_r - bk.realized_r for lv, bk in matched if lv.outcome != "OPEN"]
        if deltas:
            lo, hi = bootstrap_ci(deltas)
            print(f"\nlive vs backtest (per-trade R): mean {statistics.mean(deltas):+.3f}R "
                  f"95% CI [{lo:+.3f}, {hi:+.3f}] over {len(deltas)} resolved")
            if lo <= 0 <= hi:
                print("  -> no significant divergence (CI spans zero)")
            else:
                print("  -> divergence is significant; check fills/slippage/spread")

    live_net = sum(s.realized_r for s in live if s.outcome != "OPEN")
    back_net = sum(b.realized_r for b in back if b.outcome != "OPEN")
    print(f"\nnet R  live {live_net:+.1f}  |  backtest {back_net:+.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forward-test parity: compare live trades.csv with a same-period backtest"
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--ledger", default=None,
                        help="path to trades.csv (default: data dir)")
    parser.add_argument("--days", type=int, default=None,
                        help="how many prior days of history to load (default: span of ledger + buffer)")
    parser.add_argument("--spread", type=float, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    log = logging.getLogger("orb.forward")

    with open(config_path(args.config), encoding="utf-8") as f:
        cfg = json.load(f)
    if args.spread is not None:
        cfg.setdefault("costs", {})["spread_points"] = args.spread

    ledger_path = Path(args.ledger) if args.ledger else artifact("trades.csv")
    live = read_ledger(ledger_path)
    if not live:
        print(f"no live signals found in {ledger_path}")
        raise SystemExit(1)

    start_date = min(s.date for s in live)
    end_date = max(s.date for s in live)
    span_days = (date.today() - start_date).days
    buffer = 45  # history warm-up for the opening range / trend bars
    days = args.days or max(span_days + 10, 60)

    tz = ZoneInfo(cfg["timezone"])
    log.info("ledger %s: %d signals, %s..%s", ledger_path, len(live), start_date, end_date)

    m15, m5, h1 = load_history(cfg, days + buffer)
    back, day_list, skipped = replay(
        cfg, m15, m5, h1, tz, start_date=start_date, end_date=end_date
    )

    print(f"\nForward-test parity for {cfg['symbol']} | {start_date} .. {end_date}")
    print(f"replayed {len(day_list)} sessions (skipped {skipped}), "
          f"cost {effective_cost_points(cfg):.1f} pt\n")
    compare(live, back, effective_cost_points(cfg))


if __name__ == "__main__":
    main()
