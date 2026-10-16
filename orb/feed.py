from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

from .models import Bar

log = logging.getLogger("orb.feed")

_TF = {
    1: mt5.TIMEFRAME_M1,
    5: mt5.TIMEFRAME_M5,
    15: mt5.TIMEFRAME_M15,
    30: mt5.TIMEFRAME_M30,
    60: mt5.TIMEFRAME_H1,
    240: mt5.TIMEFRAME_H4,
    1440: mt5.TIMEFRAME_D1,
}


class Mt5Feed:
    """Thin MetaTrader 5 adapter.

    MT5 timestamps are server-clock epochs (FBS = UTC+3). This class detects
    the live server offset from the last tick and converts everything to real
    UTC so session logic can use IANA timezones (America/New_York).
    """

    def __init__(self, symbol: str, fallback_offset: int = 10800):
        self.symbol = symbol
        self._offset = int(fallback_offset)
        self._offset_known = False

    def start(self) -> None:
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not mt5.symbol_select(self.symbol, True):
            mt5.shutdown()
            raise RuntimeError(f"symbol {self.symbol} not available: {mt5.last_error()}")
        info = mt5.symbol_info(self.symbol)
        ai = mt5.account_info()
        self._detect_offset()
        log.info(
            "MT5 connected | %s | server %s login %s | %s digits=%d | server offset %+ds",
            info.name,
            ai.server,
            ai.login,
            self.symbol,
            info.digits,
            self._offset,
        )

    def _detect_offset(self) -> None:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is not None:
            self._offset = int(round((tick.time - time.time()) / 60.0)) * 60
            self._offset_known = True

    @property
    def offset(self) -> int:
        return self._offset

    def ensure_connection(self) -> bool:
        """Re-establish the terminal connection if it dropped. Returns health."""
        ti = mt5.terminal_info()
        if ti is None or not ti.connected:
            log.warning("MT5 terminal not connected - reinitializing")
            if not mt5.initialize():
                log.error("reinitialize failed: %s", mt5.last_error())
                return False
            log.info("MT5 reconnected")
        self._ensure_symbol()
        return True

    def _ensure_symbol(self) -> None:
        info = mt5.symbol_info(self.symbol)
        if info is None or not info.visible:
            mt5.symbol_select(self.symbol, True)

    def bars(self, tf_minutes: int, count: int) -> list[Bar]:
        if tf_minutes not in _TF:
            raise ValueError(f"unsupported timeframe: {tf_minutes}m")
        self._ensure_symbol()
        self._detect_offset()
        offset = self._offset
        raw = mt5.copy_rates_from_pos(self.symbol, _TF[tf_minutes], 0, count)
        if raw is None:
            log.warning("copy_rates_from_pos(%dm) failed: %s", tf_minutes, mt5.last_error())
            if not self.ensure_connection():
                return []
            raw = mt5.copy_rates_from_pos(self.symbol, _TF[tf_minutes], 0, count)
            if raw is None:
                log.error("copy_rates_from_pos(%dm) still failing after reconnect", tf_minutes)
                return []
        now_raw = time.time() + offset
        out: list[Bar] = []
        for r in raw:
            open_raw = int(r["time"])
            if open_raw + tf_minutes * 60 > now_raw:
                continue
            out.append(
                Bar(
                    tf_minutes=tf_minutes,
                    open_time=datetime.fromtimestamp(open_raw - offset, tz=timezone.utc),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    tick_volume=int(r["tick_volume"]),
                )
            )
        out.sort(key=lambda b: b.open_time)
        return out

    def shutdown(self) -> None:
        mt5.shutdown()
