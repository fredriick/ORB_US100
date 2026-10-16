from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from orb.mt5_client import (
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    TRADE_RETCODE_DONE,
    ClientError,
    HttpMt5Client,
    _norm_bar,
    _norm_side,
    _to_epoch,
    build_client,
)

D = datetime(2026, 5, 1, 13, 30, tzinfo=timezone.utc)
D_EPOCH = int(D.timestamp())


class FakeHttp(HttpMt5Client):
    """HttpMt5Client with the transport replaced by canned responses."""

    def __init__(self, responses, cfg=None):
        super().__init__(cfg or {"api": {"base_url": "http://test:8000"}})
        self.responses = responses
        self.calls = []

    def _http(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if (method, path) in self.responses:
            return self.responses[(method, path)]
        if path in self.responses:
            return self.responses[path]
        raise ClientError(f"no canned response for {method} {path}")


class TestHelpers(unittest.TestCase):
    def test_to_epoch_numeric(self):
        self.assertEqual(_to_epoch(1746000000), 1746000000)
        self.assertEqual(_to_epoch("1746000000"), 1746000000)
        self.assertEqual(_to_epoch(1746000000.9), 1746000000)

    def test_to_epoch_iso(self):
        self.assertEqual(_to_epoch("2026-05-01T13:30:00Z"), D_EPOCH)

    def test_to_epoch_bad(self):
        self.assertEqual(_to_epoch("not-a-date"), 0)
        self.assertEqual(_to_epoch(None), 0)

    def test_norm_bar_aliases(self):
        bar = _norm_bar({"t": 1746000000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10})
        self.assertEqual(bar["time"], 1746000000)
        self.assertEqual(bar["tick_volume"], 10)
        self.assertAlmostEqual(bar["close"], 1.5)

    def test_norm_side(self):
        self.assertEqual(_norm_side("buy"), ORDER_TYPE_BUY)
        self.assertEqual(_norm_side("SELL"), ORDER_TYPE_SELL)
        self.assertEqual(_norm_side(1), ORDER_TYPE_SELL)
        self.assertIsNone(_norm_side("sideways"))


class TestBuildClient(unittest.TestCase):
    def test_http_transport(self):
        client = build_client({"transport": "http", "api": {"base_url": "http://x:1"}})
        self.assertIsInstance(client, HttpMt5Client)
        self.assertEqual(client.base_url, "http://x:1")

    def test_default_is_local(self):
        try:
            import MetaTrader5  # noqa: F401
        except ImportError:
            self.skipTest("MetaTrader5 not installed")
        self.assertEqual(type(build_client({})).__name__, "LocalMt5Client")

    def test_unknown_transport_raises(self):
        with self.assertRaises(ValueError):
            build_client({"transport": "carrier-pigeon"})

    def test_env_base_url_overrides_config(self):
        os.environ["ORB_API_BASE_URL"] = "http://env:9"
        try:
            client = build_client({"transport": "http", "api": {"base_url": "http://cfg:1"}})
            self.assertEqual(client.base_url, "http://env:9")
        finally:
            del os.environ["ORB_API_BASE_URL"]


class TestHttpParsing(unittest.TestCase):
    def test_symbol_info(self):
        client = FakeHttp({"/api/v1/symbols/US100": {
            "name": "US100", "digits": 2, "point": 0.01, "volume_min": 0.01,
            "volume_max": 500, "volume_step": 0.01, "filling_mode": 1,
            "trade_tick_value": 0.1, "trade_tick_size": 0.01,
        }})
        info = client.symbol_info("US100")
        self.assertEqual(info.digits, 2)
        self.assertEqual(info.volume_min, 0.01)
        self.assertAlmostEqual(info.trade_tick_value, 0.1)

    def test_account_info(self):
        client = FakeHttp({"/api/v1/terminal/account/info": {
            "server": "FBS-Demo", "login": 106512133, "equity": 85.41,
        }})
        ai = client.account_info()
        self.assertEqual(ai.server, "FBS-Demo")
        self.assertAlmostEqual(ai.equity, 85.41)

    def test_health_from_health_endpoint(self):
        client = FakeHttp({"/health": {"connected": True, "trade_allowed": True}})
        self.assertTrue(client.health().connected)

    def test_health_falls_back_to_terminal_info(self):
        client = FakeHttp({"/api/v1/terminal/info": {"connected": False}})
        h = client.health()
        self.assertFalse(h.connected)

    def test_tick(self):
        client = FakeHttp({"/api/v1/symbols/ticks/US100": {"time": 1746000000, "bid": 1.0, "ask": 1.2}})
        t = client.tick("US100")
        self.assertEqual(t.ask, 1.2)

    def test_rates_parsing_and_params(self):
        client = FakeHttp({"/api/v1/symbols/rates/pos": [
            {"time": 1746000000, "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 7},
        ]})
        bars = client.rates("US100", 5, 10)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["tick_volume"], 7)
        method, path, params, _ = client.calls[0]
        self.assertEqual(params["timeframe"], "M5")
        self.assertEqual(params["count"], 10)

    def test_rates_range_sends_epochs(self):
        client = FakeHttp({"/api/v1/symbols/rates/range": []})
        client.rates_range("US100", 60, D, D)
        _, _, params, _ = client.calls[0]
        self.assertEqual(params["start"], D_EPOCH)
        self.assertEqual(params["timeframe"], "H1")

    def test_positions_filter_magic(self):
        client = FakeHttp({"/api/v1/positions/by_symbol/US100": [
            {"ticket": 5, "type": "buy", "volume": 0.01, "price_open": 100.0,
             "sl": 90.0, "tp": 120.0, "magic": 20261014},
        ]})
        pos = client.positions("US100")
        self.assertEqual(pos[0].magic, 20261014)
        self.assertEqual(pos[0].type, ORDER_TYPE_BUY)

    def test_order_send_maps_side(self):
        client = FakeHttp({"/api/v1/trading/order": {"retcode": TRADE_RETCODE_DONE, "price": 100.5, "order": 42}})
        res = client.order_send({"symbol": "US100", "type": ORDER_TYPE_BUY, "volume": 0.01,
                                 "price": 100.4, "sl": 90, "tp": 120, "magic": 20261014})
        self.assertEqual(res.retcode, TRADE_RETCODE_DONE)
        self.assertEqual(res.order, 42)
        _, _, _, body = client.calls[0]
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["magic"], 20261014)

    def test_order_send_sell_side(self):
        client = FakeHttp({"/api/v1/trading/order": {"success": True}})
        res = client.order_send({"symbol": "US100", "type": ORDER_TYPE_SELL, "volume": 0.01})
        self.assertEqual(res.retcode, TRADE_RETCODE_DONE)
        self.assertEqual(client.calls[0][3]["side"], "sell")

    def test_order_check_tradeable(self):
        client = FakeHttp({"/api/v1/trading/order_check/US100": {"tradeable": True}})
        res = client.order_check({"symbol": "US100", "volume": 0.01})
        self.assertEqual(res.retcode, 0)

    def test_order_check_not_tradeable(self):
        client = FakeHttp({"/api/v1/trading/order_check/US100": {"tradeable": False}})
        res = client.order_check({"symbol": "US100", "volume": 0.01})
        self.assertNotEqual(res.retcode, 0)

    def test_rates_missing_returns_none(self):
        client = FakeHttp({"/api/v1/symbols/rates/pos": None})
        self.assertIsNone(client.rates("US100", 5, 10))


if __name__ == "__main__":
    unittest.main()
