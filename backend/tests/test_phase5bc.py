"""
Unit tests Phase 5b/5c — GET /api/trading/chart-data (Terminal UI chart feed).
No real exchange/network calls: binance_service.fetch_ohlcv is monkeypatched.
Mirrors the config-stubbing pattern used in test_phase3.py / test_phase4.py.
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

if "app" not in sys.modules:
    app_mod = types.ModuleType("app")
    app_mod.__path__ = [os.path.join(BACKEND, "app")]
    sys.modules["app"] = app_mod

if "app.config" not in sys.modules:
    cfg_mod = types.ModuleType("app.config")
    settings_stub = MagicMock()
    settings_stub.active_exchange = "binance"
    cfg_mod.settings = settings_stub
    sys.modules["app.config"] = cfg_mod

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.routers.trading_v2 import router as trading_router  # noqa: E402


def _synthetic_candles(n=60, start_price=100.0):
    """Deterministic OHLCV series with a clean up-then-down swing structure."""
    candles = []
    price = start_price
    for i in range(n):
        direction = 1 if i < n // 2 else -1
        step = 0.5 * direction
        o = price
        c = price + step
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        candles.append({
            "timestamp": f"2026-01-01T{i % 24:02d}:00:00+00:00",
            "open": o, "high": h, "low": l, "close": c, "volume": 100.0,
        })
        price = c
    return candles


class TestChartDataEndpoint(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(trading_router)
        self.client = TestClient(app)

    def test_returns_candles_and_smc_levels_shape(self):
        candles = _synthetic_candles()
        with patch("app.services.binance_service.fetch_ohlcv", return_value=candles):
            resp = self.client.get("/api/trading/chart-data", params={"symbol": "BTC/USDT", "timeframe": "1h"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["symbol"], "BTC/USDT")
        self.assertEqual(body["timeframe"], "1h")
        self.assertEqual(len(body["candles"]), len(candles))
        self.assertIn("key_levels", body["smc_levels"])
        self.assertIn("confluence", body["smc_levels"])
        self.assertIn("score", body["smc_levels"]["confluence"])

    def test_key_levels_have_required_fields(self):
        candles = _synthetic_candles()
        with patch("app.services.binance_service.fetch_ohlcv", return_value=candles):
            resp = self.client.get("/api/trading/chart-data", params={"symbol": "ETH/USDT", "timeframe": "4h"})

        body = resp.json()
        for level in body["smc_levels"]["key_levels"]:
            for key in ("type", "price", "low", "high", "tf", "strength"):
                self.assertIn(key, level)
            self.assertEqual(level["tf"], "4H")

    def test_empty_candles_returns_empty_but_valid_shape(self):
        with patch("app.services.binance_service.fetch_ohlcv", return_value=[]):
            resp = self.client.get("/api/trading/chart-data", params={"symbol": "DOGE/USDT", "timeframe": "15m"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["candles"], [])
        self.assertEqual(body["smc_levels"]["key_levels"], [])
        self.assertEqual(body["smc_levels"]["confluence"]["score"], 0)

    def test_exchange_error_returns_502(self):
        with patch("app.services.binance_service.fetch_ohlcv", side_effect=RuntimeError("network down")):
            resp = self.client.get("/api/trading/chart-data", params={"symbol": "BTC/USDT", "timeframe": "1h"})

        self.assertEqual(resp.status_code, 502)

    def test_timeframe_label_mapping(self):
        candles = _synthetic_candles()
        cases = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
        for tf_in, tf_label in cases.items():
            with patch("app.services.binance_service.fetch_ohlcv", return_value=candles):
                resp = self.client.get("/api/trading/chart-data", params={"symbol": "BTC/USDT", "timeframe": tf_in})
            body = resp.json()
            levels = body["smc_levels"]["key_levels"]
            if levels:
                self.assertEqual(levels[0]["tf"], tf_label)

    def test_default_timeframe_and_limit(self):
        candles = _synthetic_candles()
        with patch("app.services.binance_service.fetch_ohlcv", return_value=candles) as mock_fetch:
            resp = self.client.get("/api/trading/chart-data", params={"symbol": "BTC/USDT"})

        self.assertEqual(resp.status_code, 200)
        mock_fetch.assert_called_once_with("BTC/USDT", timeframe="1h", limit=200)


if __name__ == "__main__":
    unittest.main()
