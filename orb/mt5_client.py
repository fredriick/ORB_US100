from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("orb.client")

# --- MT5 constants (stable, documented) -----------------------------------
# Kept here so the executor/feed never import MetaTrader5 directly and can run
# against either transport.
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

ORDER_TIME_GTC = 0

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_INVALID_FILL = 10030

# order_check returns retcode 0 when the request is valid (an order is not sent)
RETCODE_CHECK_OK = 0

# timeframe minutes -> MT5 name (local) / API string (http)
TF_NAME = {
    1: "M1",
    5: "M5",
    15: "M15",
    30: "M30",
    60: "H1",
    240: "H4",
    1440: "D1",
}


class ClientError(RuntimeError):
    """Raised when a transport call fails before a normal result is produced."""


# --- normalized data types -------------------------------------------------
@dataclass(frozen=True)
class SymbolInfo:
    name: str
    digits: int
    point: float
    visible: bool
    volume_min: float
    volume_max: float
    volume_step: float
    filling_mode: int
    trade_tick_value: float
    trade_tick_size: float


@dataclass(frozen=True)
class AccountInfo:
    server: str
    login: int
    equity: float


@dataclass(frozen=True)
class Tick:
    time: int  # server-clock epoch seconds
    bid: float
    ask: float


@dataclass(frozen=True)
class Position:
    ticket: int
    type: int  # 0 = buy, 1 = sell
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int


@dataclass(frozen=True)
class OrderCheckResult:
    retcode: int
    margin: float = 0.0
    margin_free: float = 0.0
    comment: str = ""


@dataclass(frozen=True)
class OrderResult:
    retcode: int
    price: float = 0.0
    order: int = 0
    comment: str = ""


@dataclass(frozen=True)
class Health:
    connected: bool
    trade_allowed: bool = True


# --- parsing helpers -------------------------------------------------------
def _to_epoch(value) -> int:
    """Coerce epoch seconds / numeric string / ISO string into epoch seconds."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        return int(float(text))
    except ValueError:
        pass
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _first(row: dict, *keys, default=None):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _norm_bar(row: dict) -> dict:
    return {
        "time": _to_epoch(_first(row, "time", "timestamp", "t", default=0)),
        "open": float(_first(row, "open", "o", default=0.0)),
        "high": float(_first(row, "high", "h", default=0.0)),
        "low": float(_first(row, "low", "l", default=0.0)),
        "close": float(_first(row, "close", "c", default=0.0)),
        "tick_volume": int(_first(row, "tick_volume", "volume", "v", default=0)),
    }


def _norm_side(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text in ("buy", "long", "0"):
        return ORDER_TYPE_BUY
    if text in ("sell", "short", "1"):
        return ORDER_TYPE_SELL
    return None


# --- transport interface ---------------------------------------------------
class Mt5Client:
    """Primitive MT5 operations; implemented locally and over the HTTP API."""

    def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def shutdown(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def last_error(self) -> str:  # pragma: no cover - interface
        return ""

    def symbol_info(self, symbol: str) -> SymbolInfo | None:  # pragma: no cover
        raise NotImplementedError

    def select_symbol(self, symbol: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def account_info(self) -> AccountInfo:  # pragma: no cover
        raise NotImplementedError

    def health(self) -> Health:  # pragma: no cover
        raise NotImplementedError

    def ensure_algo_trading(self) -> bool:  # pragma: no cover
        return True

    def tick(self, symbol: str) -> Tick | None:  # pragma: no cover
        raise NotImplementedError

    def rates(self, symbol: str, tf_minutes: int, count: int) -> list[dict] | None:
        raise NotImplementedError

    def rates_range(self, symbol: str, tf_minutes: int, start, end) -> list[dict] | None:
        raise NotImplementedError

    def positions(self, symbol: str) -> list[Position]:  # pragma: no cover
        raise NotImplementedError

    def order_check(self, request: dict) -> OrderCheckResult | None:  # pragma: no cover
        raise NotImplementedError

    def order_send(self, request: dict) -> OrderResult | None:  # pragma: no cover
        raise NotImplementedError

    def modify_position(self, ticket: int, symbol: str, sl: float, tp: float) -> OrderResult | None:
        raise NotImplementedError


# --- local (MetaTrader5 package, Windows) ----------------------------------
class LocalMt5Client(Mt5Client):
    """Direct MetaTrader5 IPC. The package is imported lazily so this module
    can be imported (and the HTTP client used) on machines without it."""

    def __init__(self):
        import MetaTrader5 as mt5  # noqa: N813 - lazy, Windows-only dependency

        self._mt5 = mt5

    def start(self) -> None:
        if not self._mt5.initialize():
            raise ClientError(f"MT5 initialize failed: {self._mt5.last_error()}")

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def last_error(self) -> str:
        return str(self._mt5.last_error())

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        info = self._mt5.symbol_info(symbol)
        if info is None:
            return None
        return SymbolInfo(
            name=info.name,
            digits=int(info.digits),
            point=float(info.point),
            visible=bool(info.visible),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            filling_mode=int(info.filling_mode),
            trade_tick_value=float(info.trade_tick_value),
            trade_tick_size=float(info.trade_tick_size),
        )

    def select_symbol(self, symbol: str) -> bool:
        return bool(self._mt5.symbol_select(symbol, True))

    def account_info(self) -> AccountInfo:
        ai = self._mt5.account_info()
        if ai is None:
            raise ClientError(f"account_info failed: {self._mt5.last_error()}")
        return AccountInfo(server=str(ai.server), login=int(ai.login), equity=float(ai.equity))

    def health(self) -> Health:
        ti = self._mt5.terminal_info()
        if ti is None:
            return Health(connected=False, trade_allowed=False)
        return Health(connected=bool(ti.connected), trade_allowed=bool(ti.trade_allowed))

    def tick(self, symbol: str) -> Tick | None:
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return Tick(time=int(tick.time), bid=float(tick.bid), ask=float(tick.ask))

    def _tf(self, tf_minutes: int):
        if tf_minutes not in TF_NAME:
            raise ValueError(f"unsupported timeframe: {tf_minutes}m")
        return getattr(self._mt5, "TIMEFRAME_" + TF_NAME[tf_minutes])

    @staticmethod
    def _rows(raw) -> list[dict] | None:
        if raw is None:
            return None
        return [
            {
                "time": int(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
            }
            for r in raw
        ]

    def rates(self, symbol: str, tf_minutes: int, count: int) -> list[dict] | None:
        return self._rows(self._mt5.copy_rates_from_pos(symbol, self._tf(tf_minutes), 0, count))

    def rates_range(self, symbol: str, tf_minutes: int, start, end) -> list[dict] | None:
        return self._rows(self._mt5.copy_rates_range(symbol, self._tf(tf_minutes), start, end))

    def positions(self, symbol: str) -> list[Position]:
        raw = self._mt5.positions_get(symbol=symbol) or []
        return [
            Position(
                ticket=int(p.ticket),
                type=int(p.type),
                volume=float(p.volume),
                price_open=float(p.price_open),
                sl=float(p.sl),
                tp=float(p.tp),
                magic=int(p.magic),
            )
            for p in raw
        ]

    def order_check(self, request: dict) -> OrderCheckResult | None:
        res = self._mt5.order_check(request)
        if res is None:
            return None
        return OrderCheckResult(
            retcode=int(res.retcode),
            margin=float(getattr(res, "margin", 0.0)),
            margin_free=float(getattr(res, "margin_free", 0.0)),
            comment=str(getattr(res, "comment", "")),
        )

    def order_send(self, request: dict) -> OrderResult | None:
        res = self._mt5.order_send(request)
        if res is None:
            return None
        return OrderResult(
            retcode=int(res.retcode),
            price=float(getattr(res, "price", 0.0)),
            order=int(getattr(res, "order", 0)),
            comment=str(getattr(res, "comment", "")),
        )

    def modify_position(self, ticket: int, symbol: str, sl: float, tp: float) -> OrderResult | None:
        request = {
            "action": TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": symbol,
            "sl": sl,
            "tp": tp,
        }
        return self.order_send(request)


# --- remote (nodalytics/metatrader-terminal REST API) ----------------------
class HttpMt5Client(Mt5Client):
    """HTTP transport to a dockerized MT5 terminal.

    Endpoints follow the nodalytics/metatrader-terminal API. Request/response
    field names are parsed tolerantly (several aliases accepted) because the
    exact schema is confirmed from the terminal's /docs at deployment time.
    """

    def __init__(self, cfg: dict):
        api = cfg.get("api", {})
        self.base_url = (
            os.environ.get("ORB_API_BASE_URL") or api.get("base_url", "http://mt5-terminal:8000")
        ).rstrip("/")
        self.timeout = float(api.get("timeout_sec", 10.0))
        self.api_key = os.environ.get(api.get("key_env", "ORB_API_KEY"), "")
        self.seed = os.environ.get(api.get("seed_env", "ORB_API_SEED"), "")
        self._last_error = ""

    # -- transport plumbing --
    def _http(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            self._last_error = f"HTTP {exc.code} {exc.reason} ({method} {path})"
            raise ClientError(self._last_error) from exc
        except Exception as exc:  # noqa: BLE001 - surface as a transport error
            self._last_error = f"{type(exc).__name__}: {exc} ({method} {path})"
            raise ClientError(self._last_error) from exc
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _login(self) -> None:
        if self.api_key or not self.seed:
            return
        data = self._http("POST", "/api/v1/auth/login", body={"seed": self.seed})
        if isinstance(data, dict):
            key = _first(data, "api_key", "key", "token", "access_token")
            if key:
                self.api_key = str(key)
                log.info("authenticated with MT5 terminal API")
                return
        log.warning("API login did not return a key; continuing unauthenticated")

    def start(self) -> None:
        self._login()
        if self.api_key:
            self._http("POST", "/api/v1/terminal/algo-trading", body={"enabled": True})
        health = self.health()
        if not health.connected:
            log.warning("terminal API reports not connected at startup")

    def shutdown(self) -> None:
        return None

    def last_error(self) -> str:
        return self._last_error

    # -- reads --
    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        try:
            data = self._http("GET", f"/api/v1/symbols/{urllib.parse.quote(symbol)}")
        except ClientError:
            return None
        if not isinstance(data, dict):
            return None
        digits = int(_first(data, "digits", default=2))
        point = float(_first(data, "point", default=10 ** -digits))
        return SymbolInfo(
            name=str(_first(data, "name", "symbol", default=symbol)),
            digits=digits,
            point=point,
            visible=bool(_first(data, "visible", default=True)),
            volume_min=float(_first(data, "volume_min", "min_volume", default=0.01)),
            volume_max=float(_first(data, "volume_max", "max_volume", default=500.0)),
            volume_step=float(_first(data, "volume_step", "volume_step_size", default=0.01)),
            filling_mode=int(_first(data, "filling_mode", default=1)),
            trade_tick_value=float(_first(data, "trade_tick_value", "tick_value", default=0.0)),
            trade_tick_size=float(_first(data, "trade_tick_size", "tick_size", default=point)),
        )

    def select_symbol(self, symbol: str) -> bool:
        try:
            self._http("POST", f"/api/v1/symbols/{urllib.parse.quote(symbol)}/select",
                       body={"visible": True})
            return True
        except ClientError:
            return True  # selection is implicit in most API deployments

    def account_info(self) -> AccountInfo:
        data = self._http("GET", "/api/v1/terminal/account/info")
        if not isinstance(data, dict):
            raise ClientError("account/info returned no data")
        return AccountInfo(
            server=str(_first(data, "server", "broker_server", default="")),
            login=int(_first(data, "login", "account", default=0)),
            equity=float(_first(data, "equity", default=0.0)),
        )

    def health(self) -> Health:
        data = None
        try:
            data = self._http("GET", "/health")
        except ClientError:
            data = None
        if not isinstance(data, dict):
            try:
                data = self._http("GET", "/api/v1/terminal/info")
            except ClientError:
                return Health(connected=False, trade_allowed=False)
        if not isinstance(data, dict):
            return Health(connected=False, trade_allowed=False)
        connected = bool(_first(data, "connected", "terminal_connected", default=False))
        trade_allowed = bool(_first(data, "trade_allowed", "trading_allowed", default=True))
        return Health(connected=connected, trade_allowed=trade_allowed)

    def ensure_algo_trading(self) -> bool:
        try:
            self._http("POST", "/api/v1/terminal/algo-trading", body={"enabled": True})
            return True
        except ClientError as exc:
            log.warning("could not enable algo trading: %s", exc)
            return False

    def tick(self, symbol: str) -> Tick | None:
        try:
            data = self._http("GET", f"/api/v1/symbols/ticks/{urllib.parse.quote(symbol)}")
        except ClientError:
            return None
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return Tick(
            time=_to_epoch(_first(data, "time", "timestamp", default=0)),
            bid=float(_first(data, "bid", "b", default=0.0)),
            ask=float(_first(data, "ask", "a", default=0.0)),
        )

    def rates(self, symbol: str, tf_minutes: int, count: int) -> list[dict] | None:
        if tf_minutes not in TF_NAME:
            raise ValueError(f"unsupported timeframe: {tf_minutes}m")
        try:
            data = self._http(
                "GET",
                "/api/v1/symbols/rates/pos",
                params={"symbol": symbol, "timeframe": TF_NAME[tf_minutes], "count": count},
            )
        except ClientError:
            return None
        return self._parse_rates(data)

    def rates_range(self, symbol: str, tf_minutes: int, start, end) -> list[dict] | None:
        if tf_minutes not in TF_NAME:
            raise ValueError(f"unsupported timeframe: {tf_minutes}m")
        try:
            data = self._http(
                "GET",
                "/api/v1/symbols/rates/range",
                params={
                    "symbol": symbol,
                    "timeframe": TF_NAME[tf_minutes],
                    "start": int(_as_epoch(start)),
                    "end": int(_as_epoch(end)),
                },
            )
        except ClientError:
            return None
        return self._parse_rates(data)

    @staticmethod
    def _parse_rates(data) -> list[dict] | None:
        if data is None:
            return None
        if isinstance(data, dict):
            data = _first(data, "rates", "data", "bars", "candles", default=[])
        if not isinstance(data, list):
            return None
        return [_norm_bar(row) for row in data]

    def positions(self, symbol: str) -> list[Position]:
        try:
            data = self._http("GET", f"/api/v1/positions/by_symbol/{urllib.parse.quote(symbol)}")
        except ClientError:
            return []
        if isinstance(data, dict):
            data = _first(data, "positions", "data", default=[])
        if not isinstance(data, list):
            return []
        out = []
        for p in data:
            if not isinstance(p, dict):
                continue
            out.append(
                Position(
                    ticket=int(_first(p, "ticket", "id", default=0)),
                    type=_norm_side(_first(p, "type", "side", default=0)) or ORDER_TYPE_BUY,
                    volume=float(_first(p, "volume", "lots", default=0.0)),
                    price_open=float(_first(p, "price_open", "open_price", "price", default=0.0)),
                    sl=float(_first(p, "sl", default=0.0) or 0.0),
                    tp=float(_first(p, "tp", default=0.0) or 0.0),
                    magic=int(_first(p, "magic", default=0) or 0),
                )
            )
        return out

    # -- trading --
    def order_check(self, request: dict) -> OrderCheckResult | None:
        symbol = request.get("symbol", "")
        try:
            data = self._http("GET", f"/api/v1/trading/order_check/{urllib.parse.quote(symbol)}")
        except ClientError:
            return None
        if isinstance(data, dict) and "retcode" in data:
            return OrderCheckResult(
                retcode=int(data["retcode"]),
                margin=float(_first(data, "margin", default=0.0)),
                margin_free=float(_first(data, "margin_free", default=0.0)),
                comment=str(_first(data, "comment", default="")),
            )
        tradeable = True
        if isinstance(data, dict):
            tradeable = bool(_first(data, "tradeable", "trade_allowed", "ok", default=True))
        return OrderCheckResult(
            retcode=RETCODE_CHECK_OK if tradeable else 1,
            margin=float(request.get("volume", 0.0)),
            comment="tradeable" if tradeable else "not tradeable",
        )

    def order_send(self, request: dict) -> OrderResult | None:
        side = "buy" if request.get("type") == ORDER_TYPE_BUY else "sell"
        body = {
            "symbol": request.get("symbol"),
            "side": side,
            "type": side,
            "volume": request.get("volume"),
            "price": request.get("price"),
            "sl": request.get("sl"),
            "tp": request.get("tp"),
            "deviation": request.get("deviation"),
            "magic": request.get("magic"),
            "comment": request.get("comment", "ORB"),
        }
        try:
            data = self._http("POST", "/api/v1/trading/order", body=body)
        except ClientError as exc:
            log.error("order_send transport error: %s", exc)
            return None
        if not isinstance(data, dict):
            return OrderResult(retcode=TRADE_RETCODE_DONE, comment="submitted")
        retcode = _first(data, "retcode")
        if retcode is None:
            ok = bool(_first(data, "success", "ok", default=True))
            retcode = TRADE_RETCODE_DONE if ok else TRADE_RETCODE_REJECT
        return OrderResult(
            retcode=int(retcode),
            price=float(_first(data, "price", default=0.0) or 0.0),
            order=int(_first(data, "order", "ticket", "id", default=0) or 0),
            comment=str(_first(data, "comment", default="")),
        )

    def modify_position(self, ticket: int, symbol: str, sl: float, tp: float) -> OrderResult | None:
        body = {"ticket": ticket, "sl": sl, "tp": tp}
        try:
            data = self._http("POST", "/api/v1/positions/modify", body=body)
        except ClientError as exc:
            log.error("modify_position transport error: %s", exc)
            return None
        if isinstance(data, dict) and data.get("retcode") is not None:
            return OrderResult(retcode=int(data["retcode"]), comment=str(data.get("comment", "")))
        return OrderResult(retcode=TRADE_RETCODE_DONE, comment="modified")


def _as_epoch(value) -> int:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return _to_epoch(value)


def build_client(cfg: dict) -> Mt5Client:
    """Build the transport selected by config `transport` (default: local)."""
    transport = str(cfg.get("transport", "local")).strip().lower()
    if transport in ("http", "api", "remote"):
        log.info("using HTTP transport -> %s", cfg.get("api", {}).get("base_url", ""))
        return HttpMt5Client(cfg)
    if transport in ("local", "mt5", "ipc"):
        return LocalMt5Client()
    raise ValueError(f"unknown transport: {transport!r} (expected 'local' or 'http')")
