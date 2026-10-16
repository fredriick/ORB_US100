from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone

from .models import Bar, Direction, Signal, Zone

log = logging.getLogger("orb.engine")


class State:
    WAITING = "waiting"
    ARMED = "armed"
    AWAIT_RETEST = "await_retest"
    SIGNAL = "signal"
    DONE = "done"


class _Pending:
    __slots__ = ("direction", "level", "trend", "breakout", "bars_seen")

    def __init__(self, direction: Direction, level: float, trend: str, breakout: Bar):
        self.direction = direction
        self.level = level
        self.trend = trend
        self.breakout = breakout
        self.bars_seen = 0


class OrbEngine:
    """Pure ORB state machine: 15m opening range -> 5m breakout -> retest entry.

    All times are timezone-aware UTC. No MT5 dependency; fully unit-testable.
    """

    def __init__(self, cfg: dict, trend_at, day: date, tz):
        self.cfg = cfg
        self.trend_at = trend_at
        self.day = day
        self.tz = tz

        self.state = State.WAITING
        self.zone: Zone | None = None
        self.signals: list[Signal] = []
        self.orb_missing = False
        self._pending: _Pending | None = None
        self._active: Signal | None = None
        self._last_bar: Bar | None = None

        open_local = datetime.combine(
            day, dtime.fromisoformat(cfg["session_open_local"]), tzinfo=tz
        )
        cutoff_local = datetime.combine(
            day, dtime.fromisoformat(cfg["session_cutoff_local"]), tzinfo=tz
        )
        self.open_utc = open_local.astimezone(timezone.utc)
        self.cutoff_utc = cutoff_local.astimezone(timezone.utc)

    def _fmt(self, dt: datetime) -> str:
        return dt.astimezone(self.tz).strftime("%H:%M")

    # ------------------------------------------------------------------ ORB bar

    def on_orb_bar(self, bar: Bar) -> None:
        if self.state != State.WAITING:
            return
        z = Zone(high=bar.high, low=bar.low, open_time=bar.open_time)
        min_range = float(self.cfg.get("min_zone_range", 0.0))
        if z.range < min_range:
            self.state = State.DONE
            log.info(
                "[%s] opening range too small: %.2f < min %.2f - no trade today",
                self.day,
                z.range,
                min_range,
            )
            return
        self.zone = z
        self.state = State.ARMED
        log.info(
            "[%s] ORB zone %s-%s marked (range %.1f)",
            self.day,
            f"{z.high:.2f}",
            f"{z.low:.2f}",
            z.range,
        )

    # ------------------------------------------------------------------ 5m bars

    def on_5m_bar(self, bar: Bar) -> None:
        if self.state in (State.WAITING, State.DONE):
            return
        if self.state == State.SIGNAL:
            self._track(bar)
            return
        if bar.open_time >= self.cutoff_utc:
            log.info(
                "[%s] session cutoff reached (%s) - stop for the day",
                self.day,
                self._fmt(self.cutoff_utc),
            )
            self.state = State.DONE
            self._pending = None
            return
        if self.state == State.ARMED:
            self._check_breakout(bar)
        elif self.state == State.AWAIT_RETEST:
            self._check_retest(bar)
        if self.state == State.SIGNAL:
            self._track(bar)

    def _allowed(self, direction: Direction, trend: str) -> bool:
        tcfg = self.cfg.get("trend", {})
        if not tcfg.get("enabled", True):
            return True
        if trend == "up":
            return direction == Direction.LONG
        if trend == "down":
            return direction == Direction.SHORT
        return True

    def _check_breakout(self, bar: Bar) -> None:
        z = self.zone
        if bar.close > z.high:
            direction, level = Direction.LONG, z.high
        elif bar.close < z.low:
            direction, level = Direction.SHORT, z.low
        else:
            return
        trend = self.trend_at(bar.open_time)
        if not self._allowed(direction, trend):
            log.info(
                "[%s] %s breakout close %.2f ignored (higher-TF trend: %s)",
                self.day,
                direction.value,
                bar.close,
                trend,
            )
            return
        self._pending = _Pending(direction, level, trend, bar)
        self.state = State.AWAIT_RETEST
        log.info(
            "[%s] %s BREAKOUT: 5m close %.2f beyond %.2f at %s (trend %s) - awaiting retest",
            self.day,
            direction.value.upper(),
            bar.close,
            level,
            self._fmt(bar.open_time),
            trend,
        )

    def _check_retest(self, bar: Bar) -> None:
        p = self._pending
        buf = float(self.cfg.get("retest_buffer", 0.0))
        p.bars_seen += 1

        # A limit order at the boundary fills only when price actually reaches it;
        # if price merely comes within `buf` we fill at the approached price (no optimism).
        if p.direction == Direction.LONG and bar.low <= p.level + buf:
            fill = p.level if bar.low <= p.level else bar.low
            self._emit(p, bar, fill)
            return
        if p.direction == Direction.SHORT and bar.high >= p.level - buf:
            fill = p.level if bar.high >= p.level else bar.high
            self._emit(p, bar, fill)
            return

        timeout = int(self.cfg.get("retest_timeout_bars", 12))
        if p.bars_seen >= timeout:
            self._pending = None
            if self.cfg.get("allow_rearm", False):
                self.state = State.ARMED
                log.info(
                    "[%s] no retest within %d bars - re-arming", self.day, timeout
                )
            else:
                self.state = State.DONE
                log.info(
                    "[%s] no retest within %d bars - done for the day",
                    self.day,
                    timeout,
                )

    # ------------------------------------------------------------------ signal

    def _emit(self, p: _Pending, bar: Bar, fill: float) -> None:
        max_signals = int(self.cfg.get("max_signals_per_day", 1))
        if len(self.signals) >= max_signals:
            self.state = State.DONE
            return
        z = self.zone
        sl_mode = self.cfg.get("sl_mode", "zone_opposite")
        sl_buffer = float(self.cfg.get("sl_buffer", 0.0))
        if p.direction == Direction.LONG:
            base = z.low if sl_mode == "zone_opposite" else bar.low
            sl = base - sl_buffer
        else:
            base = z.high if sl_mode == "zone_opposite" else bar.high
            sl = base + sl_buffer

        entry_mode = self.cfg.get("entry_mode", "limit")
        entry = fill if entry_mode == "limit" else bar.close

        # Enforce a sane minimum stop distance: a retest swing that is tighter than
        # the configurable floor (or inside the spread) is widened to the floor.
        min_risk = float(self.cfg.get("min_risk_points", 0.0))
        if min_risk > 0 and abs(entry - sl) < min_risk:
            sl = entry - min_risk if p.direction == Direction.LONG else entry + min_risk

        risk = abs(entry - sl)
        if risk <= 0:
            log.error(
                "[%s] invalid setup: entry %.2f vs sl %.2f - dropping signal",
                self.day,
                entry,
                sl,
            )
            self.state = State.DONE
            return
        tp_r = float(self.cfg.get("tp_r", 2.0))
        tp = entry + tp_r * risk if p.direction == Direction.LONG else entry - tp_r * risk

        sig = Signal(
            day=self.day,
            direction=p.direction,
            entry_mode=entry_mode,
            entry=round(entry, 2),
            sl=round(sl, 2),
            tp=round(tp, 2),
            zone_high=z.high,
            zone_low=z.low,
            trend=p.trend,
            breakout_time=p.breakout.open_time,
            signal_time=bar.open_time,
        )
        self.signals.append(sig)
        self._active = sig
        self.state = State.SIGNAL
        log.info(
            "[%s] >>> %s SIGNAL | entry %.2f | SL %.2f | TP %.2f | risk %.1f pts | trend %s",
            self.day,
            p.direction.value.upper(),
            sig.entry,
            sig.sl,
            sig.tp,
            sig.risk,
            p.trend,
        )

    def _track(self, bar: Bar) -> None:
        s = self._active
        if s is None or s.outcome != "OPEN":
            return
        self._last_bar = bar
        if s.direction == Direction.LONG:
            if bar.low <= s.sl:
                self._resolve(s, "SL", bar, s.sl)
                return
            if bar.high >= s.tp:
                self._resolve(s, "TP", bar, s.tp)
                return
        else:
            if bar.high >= s.sl:
                self._resolve(s, "SL", bar, s.sl)
                return
            if bar.low <= s.tp:
                self._resolve(s, "TP", bar, s.tp)
                return
        if self.cfg.get("exit_at_cutoff", True) and bar.open_time >= self.cutoff_utc:
            self._resolve(s, "EOD", bar, bar.close)

    def _resolve(self, s: Signal, outcome: str, bar: Bar, price: float) -> None:
        s.outcome = outcome
        s.outcome_time = bar.open_time
        s.exit_price = round(price, 2)
        log.info(
            "[%s] %s %s at %s -> %s %.2f (%+.2fR)",
            s.day,
            s.direction.value.upper(),
            outcome,
            self._fmt(bar.open_time),
            "exit",
            s.exit_price,
            s.realized_r,
        )

    def finish_day(self) -> None:
        if self._active is not None and self._active.outcome == "OPEN":
            if self._last_bar is not None:
                self._resolve(self._active, "EOD", self._last_bar, self._last_bar.close)
            else:
                log.info(
                    "[%s] %s signal never filled/tracked - marked EXPIRED",
                    self._active.day,
                    self._active.direction.value.upper(),
                )
                self._active.outcome = "EXPIRED"
        self.state = State.DONE
        self._pending = None
