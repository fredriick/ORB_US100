from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .models import Bar
from .mt5_client import Mt5Client, TF_NAME, build_client

log = logging.getLogger("orb.feed")


class Mt5Feed:
    """Market-data adapter over an Mt5Client (local IPC or HTTP API).

    MT5 timestamps are server-clock epochs (FBS = UTC+3). This class detects
    the live server offset from the last tick and converts everything to real
    UTC so session logic can use IANA timezones (America/New_York).
    """

    def __init__(self, symbol: str, fallback_offset: int = 10800, client: Mt5Client | None = None):
        self.symbol = symbol
        self._offset = int(fallback_offset)
        self._offset_known = False
        self.client = client or build_client({"transport": "local"})

    def start(self) -> None:
        self.client.start()
        info = self.client.symbol_info(self.symbol)
        if info is None or not info.visible:
            self.client.select_symbol(self.symbol)
            info = self.client.symbol_info(self.symbol)
        if info is None:
            self.client.shutdown()
            raise RuntimeError(f"symbol {self.symbol} not available: {self.client.last_error()}")
        try:
            ai = self.client.account_info()
            server, login = ai.server, ai.login
        except Exception:  # noqa: BLE001 - server/login are cosmetic
            server, login = "?", 0
        self._detect_offset()
        log.info(
            "MT5 connected | %s | server %s login %s | %s digits=%d | server offset %+ds",
            info.name,
            server,
            login,
            self.symbol,
            info.digits,
            self._offset,
        )

    def _detect_offset(self) -> None:
        tick = self.client.tick(self.symbol)
        if tick is not None and tick.time:
            self._offset = int(round((tick.time - time.time()) / 60.0)) * 60
            self._offset_known = True

    @property
    def offset(self) -> int:
        return self._offset

    def ensure_connection(self) -> bool:
        """Re-establish the terminal connection if it dropped. Returns health."""
        health = self.client.health()
        if not health.connected:
            log.warning("MT5 terminal not connected - reinitializing")
            try:
                self.client.start()
            except Exception as exc:  # noqa: BLE001
                log.error("reinitialize failed: %s", exc)
                return False
            log.info("MT5 reconnected")
        self._ensure_symbol()
        return True

    def _ensure_symbol(self) -> None:
        info = self.client.symbol_info(self.symbol)
        if info is None or not info.visible:
            self.client.select_symbol(self.symbol)

    def _convert(self, raw, tf_minutes: int, offset: int) -> list[Bar]:
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

    def bars(self, tf_minutes: int, count: int) -> list[Bar]:
        if tf_minutes not in TF_NAME:
            raise ValueError(f"unsupported timeframe: {tf_minutes}m")
        self._ensure_symbol()
        self._detect_offset()
        offset = self._offset
        raw = self.client.rates(self.symbol, tf_minutes, count)
        if raw is None:
            log.warning("rates(%dm) failed: %s", tf_minutes, self.client.last_error())
            if not self.ensure_connection():
                return []
            raw = self.client.rates(self.symbol, tf_minutes, count)
            if raw is None:
                log.error("rates(%dm) still failing after reconnect", tf_minutes)
                return []
        return self._convert(raw, tf_minutes, offset)

    def bars_range(self, tf_minutes: int, start, end) -> list[Bar]:
        """Bars between two datetimes (triggers on-demand history download)."""
        if tf_minutes not in TF_NAME:
            raise ValueError(f"unsupported timeframe: {tf_minutes}m")
        self._ensure_symbol()
        self._detect_offset()
        offset = self._offset
        raw = self.client.rates_range(self.symbol, tf_minutes, start, end)
        if raw is None or len(raw) == 0:
            log.warning("rates_range(%dm) returned nothing: %s", tf_minutes, self.client.last_error())
            if self.ensure_connection():
                raw = self.client.rates_range(self.symbol, tf_minutes, start, end)
            if raw is None or len(raw) == 0:
                return []
        return self._convert(raw, tf_minutes, offset)

    def shutdown(self) -> None:
        self.client.shutdown()
