from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from orb.engine import OrbEngine
from orb.executor import Executor
from orb.feed import Mt5Feed
from orb.ledger import TradeLedger
from orb.trend import TrendProvider

BASE = Path(__file__).resolve().parent


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE / "bot.log", encoding="utf-8"),
    ):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="NAS100 ORB signal bot (MT5)")
    parser.add_argument("--config", default=str(BASE / "config.json"))
    parser.add_argument("--live", action="store_true",
                        help="override live_trading and actually place orders")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    if args.live:
        cfg["live_trading"] = True

    setup_logging()
    log = logging.getLogger("orb.main")

    if cfg.get("live_trading"):
        log.warning("LIVE TRADING ENABLED - real orders will be sent to MT5")
    else:
        log.info("signal-only mode (set live_trading=true or pass --live to trade)")

    tz = ZoneInfo(cfg["timezone"])
    feed = Mt5Feed(cfg["symbol"], int(cfg.get("server_offset_sec_fallback", 10800)))
    feed.start()
    trend = TrendProvider(feed, cfg["trend"])
    ledger = TradeLedger(str(BASE / "trades.csv"))
    executor = Executor(cfg["symbol"], cfg)

    live_from = datetime.now(timezone.utc)
    handled: set[tuple] = set()

    engine: OrbEngine | None = None
    fed15: set[datetime] = set()
    fed5: set[datetime] = set()
    no_orb_logged = False

    orb_minutes = int(cfg.get("orb_timeframe_minutes", 15))
    entry_tf = int(cfg.get("entry_timeframe_minutes", 5))
    poll = int(cfg.get("poll_seconds", 10))

    log.info(
        "ORB signal bot started | %s | open %s %s | cutoff %s | poll %ds",
        cfg["symbol"],
        cfg["session_open_local"],
        cfg["timezone"],
        cfg["session_cutoff_local"],
        poll,
    )

    try:
        while True:
            now = datetime.now(timezone.utc)
            today = now.astimezone(tz).date()

            if engine is None or engine.day != today:
                if engine is not None:
                    engine.finish_day()
                    ledger.sync(engine.signals)
                    ledger.save()
                engine = OrbEngine(cfg, trend.trend_at, today, tz)
                fed15.clear()
                fed5.clear()
                no_orb_logged = False
                log.info("=== session %s ===", today)

            orb_window_end = engine.open_utc + timedelta(minutes=orb_minutes)

            for b in feed.bars(15, 60):
                if (
                    engine.open_utc <= b.open_time < orb_window_end
                    and b.open_time not in fed15
                    and now >= b.close_time
                ):
                    engine.on_orb_bar(b)
                    fed15.add(b.open_time)

            if not fed15 and now >= orb_window_end + timedelta(minutes=1) and not no_orb_logged:
                log.warning(
                    "no 15m opening bar for %s - market holiday or no data, skipping day",
                    today,
                )
                no_orb_logged = True

            for b in feed.bars(5, 400):
                if (
                    b.open_time >= engine.open_utc
                    and b.open_time not in fed5
                    and now >= b.close_time
                ):
                    engine.on_5m_bar(b)
                    fed5.add(b.open_time)

            for sig in engine.signals:
                key = (sig.signal_time, sig.direction)
                if key in handled:
                    continue
                handled.add(key)
                retest_close = sig.signal_time + timedelta(minutes=entry_tf)
                if retest_close <= live_from:
                    log.info(
                        "replayed signal %s %s (historical, not executed)",
                        sig.day,
                        sig.direction.value.upper(),
                    )
                    continue
                executor.execute(sig)

            ledger.sync(engine.signals)
            ledger.save()
            time.sleep(poll)
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        if engine is not None:
            engine.finish_day()
            ledger.sync(engine.signals)
            ledger.save()
        feed.shutdown()


if __name__ == "__main__":
    main()
