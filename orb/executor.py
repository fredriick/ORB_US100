from __future__ import annotations

import logging
import math

from .models import Direction, Signal
from .mt5_client import (
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    ORDER_TIME_GTC,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    TRADE_ACTION_DEAL,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_INVALID_FILL,
    TRADE_RETCODE_REJECT,
    Mt5Client,
    build_client,
)

log = logging.getLogger("orb.exec")


def _decimals(step: float) -> int:
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def calc_lot(
    equity: float,
    risk_percent: float,
    sl_distance: float,
    tick_value: float,
    tick_size: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> float:
    """Position size so that (sl_distance) costs risk_percent of equity.

    Returns 0.0 when the risk budget cannot fund the minimum lot.
    """
    if sl_distance <= 0 or tick_size <= 0 or risk_percent <= 0 or tick_value <= 0:
        return 0.0
    value_per_price_unit = tick_value / tick_size
    per_lot_risk = sl_distance * value_per_price_unit
    if per_lot_risk <= 0:
        return 0.0
    raw = (equity * risk_percent / 100.0) / per_lot_risk
    lots = math.floor(raw / volume_step) * volume_step
    lots = round(lots, _decimals(volume_step))
    if lots < volume_min:
        return 0.0
    return min(lots, volume_max)


class Executor:
    """Places real orders via an Mt5Client. No-op logging until live_trading."""

    def __init__(self, symbol: str, cfg: dict, client: Mt5Client | None = None):
        self.symbol = symbol
        self.cfg = cfg
        self.exec_cfg = cfg.get("execution", {})
        self.magic = int(self.exec_cfg.get("magic", 20261014))
        self.client = client or build_client({"transport": "local"})

    def has_position(self) -> bool:
        return any(p.magic == self.magic for p in self.client.positions(self.symbol))

    def open_positions(self) -> list:
        return [p for p in self.client.positions(self.symbol) if p.magic == self.magic]

    def _candidate_fillings(self, info) -> list[int]:
        modes = []
        if info.filling_mode & 1:
            modes.append(ORDER_FILLING_FOK)
        if info.filling_mode & 2:
            modes.append(ORDER_FILLING_IOC)
        if not modes:
            modes.append(ORDER_FILLING_RETURN)
        return modes

    def _compute_lot(self, info, sl_distance: float) -> float:
        mode = self.exec_cfg.get("lot_mode", "risk_percent")
        if mode == "fixed":
            lots = float(self.exec_cfg.get("fixed_lot", info.volume_min))
            lots = math.floor(lots / info.volume_step) * info.volume_step
            return max(info.volume_min, min(info.volume_max, round(lots, _decimals(info.volume_step))))
        equity = self.client.account_info().equity
        return calc_lot(
            equity,
            float(self.exec_cfg.get("risk_percent", 1.0)),
            sl_distance,
            info.trade_tick_value,
            info.trade_tick_size,
            info.volume_min,
            info.volume_max,
            info.volume_step,
        )

    def _reference_price(self, info) -> float | None:
        tick = self.client.tick(self.symbol)
        if tick is not None:
            price = tick.ask or tick.bid
            if price:
                return float(price)
        rates = self.client.rates(self.symbol, 1, 1)
        if rates:
            return float(rates[-1]["close"])
        return None

    def self_test(self) -> bool:
        """Validate the order pipeline with order_check (no order is sent)."""
        info = self.client.symbol_info(self.symbol)
        if info is None:
            log.error("SELF-TEST failed: unknown symbol %s", self.symbol)
            return False
        price = self._reference_price(info)
        if price is None:
            log.error("SELF-TEST failed: no price available (market closed and no history)")
            return False

        equity = self.client.account_info().equity
        lot = self._compute_lot(info, max(float(self.cfg.get("min_risk_points", 20.0)), 1.0))
        print(f"  transport   : {type(self.client).__name__}")
        print(f"  symbol      : {self.symbol} (digits {info.digits}, "
              f"vol min {info.volume_min} step {info.volume_step})")
        print(f"  account     : equity {equity:.2f} | lot_mode {self.exec_cfg.get('lot_mode')}")
        print(f"  test price  : {price}")
        print(f"  test volume : {lot}")

        all_ok = True
        for label, otype, px in (
            ("BUY", ORDER_TYPE_BUY, price + 5 * info.point),
            ("SELL", ORDER_TYPE_SELL, price - 5 * info.point),
        ):
            request = {
                "action": TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": lot if lot > 0 else info.volume_min,
                "type": otype,
                "price": px,
                "deviation": int(self.exec_cfg.get("deviation_points", 50)),
                "magic": self.magic,
                "comment": "ORB selftest",
                "type_time": ORDER_TIME_GTC,
            }
            result = None
            for filling in self._candidate_fillings(info):
                request["type_filling"] = filling
                result = self.client.order_check(request)
                if result is not None and result.retcode == 0:
                    break
            if result is None:
                print(f"  {label}: order_check returned None ({self.client.last_error()})")
                all_ok = False
            elif result.retcode == 0:
                print(f"  {label}: OK  (margin {result.margin:.2f}, "
                      f"free-after {result.margin_free:.2f})")
            else:
                print(f"  {label}: FAILED retcode {result.retcode} ({result.comment})")
                all_ok = False

        if lot <= 0:
            print("  note        : computed lot is 0 - order would be skipped at runtime")
            all_ok = False
        return all_ok

    def _dry_run(self, sig: Signal, side: str) -> None:
        info = self.client.symbol_info(self.symbol)
        tick = self.client.tick(self.symbol)
        if info is None or tick is None:
            log.info(
                "PAPER %s | plan entry %.2f SL %.2f TP %.2f | no tick (market closed), "
                "no order sent",
                side,
                sig.entry,
                sig.sl,
                sig.tp,
            )
            return
        is_long = sig.direction == Direction.LONG
        price = tick.ask if is_long else tick.bid
        risk_dist = abs(price - sig.sl)
        lot = self._compute_lot(info, risk_dist)
        log.info(
            "PAPER %s | market %.2f SL %.2f | lot %.2f | live_trading=false, no order sent",
            side,
            price,
            sig.sl,
            lot,
        )

    def execute(self, sig: Signal) -> dict | None:
        side = "LONG" if sig.direction == Direction.LONG else "SHORT"
        if not self.cfg.get("live_trading"):
            self._dry_run(sig, side)
            return None

        if self.has_position():
            log.warning("%s signal ignored: an ORB position is already open", side)
            return None

        info = self.client.symbol_info(self.symbol)
        tick = self.client.tick(self.symbol)
        if info is None or tick is None:
            log.error("%s signal ignored: no market data (market closed?)", side)
            return None

        is_long = sig.direction == Direction.LONG
        price = tick.ask if is_long else tick.bid
        sl = sig.sl
        tp_r = float(self.cfg.get("tp_r", 2.0))
        risk_dist = abs(price - sl)
        tp = price + tp_r * risk_dist if is_long else price - tp_r * risk_dist

        digits = info.digits
        price, sl, tp = round(price, digits), round(sl, digits), round(tp, digits)

        lot = self._compute_lot(info, risk_dist)
        if lot <= 0:
            equity = self.client.account_info().equity
            log.warning(
                "%s signal skipped: computed lot 0 (equity %.2f, risk distance %.2f pts, "
                "risk_percent %.2f) - increase equity/risk_percent or use fixed lot",
                side,
                equity,
                risk_dist,
                float(self.exec_cfg.get("risk_percent", 1.0)),
            )
            return None

        request = {
            "action": TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lot,
            "type": ORDER_TYPE_BUY if is_long else ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": int(self.exec_cfg.get("deviation_points", 50)),
            "magic": self.magic,
            "comment": "ORB",
            "type_time": ORDER_TIME_GTC,
        }

        last = None
        for filling in self._candidate_fillings(info):
            request["type_filling"] = filling
            last = self.client.order_send(request)
            if last is None:
                log.error("order_send returned None: %s", self.client.last_error())
                continue
            if last.retcode == TRADE_RETCODE_DONE:
                log.info(
                    "ORDER FILLED %s %.3f lot @ %.2f | SL %.2f TP %.2f | ticket %s",
                    side,
                    lot,
                    last.price,
                    sl,
                    tp,
                    last.order,
                )
                return {
                    "ticket": last.order,
                    "lot": lot,
                    "price": last.price,
                    "sl": sl,
                    "tp": tp,
                }
            if last.retcode not in (TRADE_RETCODE_INVALID_FILL, TRADE_RETCODE_REJECT):
                break

        if last is not None:
            log.error(
                "ORDER FAILED %s lot %.3f: retcode %s (%s)",
                side,
                lot,
                last.retcode,
                last.comment,
            )
        return None
