"""
Unit tests Phase 4 — backtest engine (no real exchange/network calls, no real
Claude API calls). Mirrors the stubbing pattern used in test_phase3.py so
these run without a .env file.
"""
import sys
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

# ── Stub config (no .env needed) ────────────────────────────────────────────
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()

if "app" not in sys.modules:
    app_mod = types.ModuleType("app")
    app_mod.__path__ = [os.path.join(BACKEND, "app")]
    sys.modules["app"] = app_mod

if "app.config" not in sys.modules:
    cfg_mod = types.ModuleType("app.config")
    settings_stub = MagicMock()
    cfg_mod.settings = settings_stub
    sys.modules["app.config"] = cfg_mod

# Other test modules (e.g. test_phase3) may have already stubbed app.config
# with a bare MagicMock — (re)set the concrete attributes Phase 4 needs
# regardless of who created the module first, so run order doesn't matter.
_settings = sys.modules["app.config"].settings
_settings.anthropic_api_key = "sk-test-stub"
_settings.claude_model = "claude-sonnet-4-6"
_settings.binance_api_key = ""
_settings.binance_api_secret = ""
_settings.database_url = f"sqlite:///{_TMP_DB.name}"

from app.models.database import init_db, SessionLocal, CandleCache  # noqa: E402
from app.services.backtest import smc_replay, signal_simulator, engine, data_loader  # noqa: E402

init_db()


def C(ts, o, h, l, c, v=100):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _ts(i, step_minutes=15, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
    return (start + timedelta(minutes=i * step_minutes)).isoformat()


def _flat_series(n, price=100.0, step_minutes=15):
    return [C(_ts(i, step_minutes), price, price + 1, price - 1, price) for i in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# smc_replay — walk-forward windowing, no lookahead
# ─────────────────────────────────────────────────────────────────────────────

class TestSmcReplay(unittest.TestCase):
    def test_no_lookahead_window_excludes_future_bars(self):
        # Distinct, monotonically increasing close so we can detect leakage.
        candles_15m = [C(_ts(i), i, i + 1, i - 1, i) for i in range(200)]
        candles_1h = candles_15m
        candles_4h = candles_15m
        candles_1d = candles_15m

        snapshots = list(smc_replay.replay(candles_15m, candles_1h, candles_4h, candles_1d, warmup_bars=150))
        self.assertTrue(snapshots)

        first = snapshots[0]
        as_of = first["timestamp"]
        for window_key in ("candles_15m", "candles_1h", "candles_4h", "candles_1d"):
            for c in first[window_key]:
                self.assertLessEqual(c["timestamp"], as_of, f"{window_key} leaked a future bar")

    def test_window_sizes_capped_to_live_snapshot_shape(self):
        candles_15m = [C(_ts(i), i, i, i, i) for i in range(500)]
        snapshots = list(smc_replay.replay(candles_15m, candles_15m, candles_15m, candles_15m, warmup_bars=150))
        last = snapshots[-1]
        self.assertLessEqual(len(last["candles_15m"]), smc_replay.WINDOW_15M)
        self.assertLessEqual(len(last["candles_1h"]), smc_replay.WINDOW_1H)
        self.assertLessEqual(len(last["candles_4h"]), smc_replay.WINDOW_4H)
        self.assertLessEqual(len(last["candles_1d"]), smc_replay.WINDOW_1D)

    def test_bar_index_matches_source_position(self):
        candles_15m = [C(_ts(i), i, i, i, i) for i in range(200)]
        snapshots = list(smc_replay.replay(candles_15m, candles_15m, candles_15m, candles_15m, warmup_bars=150))
        for snap in snapshots:
            self.assertEqual(candles_15m[snap["bar_index"]]["timestamp"], snap["timestamp"])

    def test_too_short_history_yields_nothing(self):
        candles_15m = [C(_ts(i), i, i, i, i) for i in range(10)]
        snapshots = list(smc_replay.replay(candles_15m, candles_15m, candles_15m, candles_15m, warmup_bars=150))
        self.assertEqual(snapshots, [])


# ─────────────────────────────────────────────────────────────────────────────
# signal_simulator — rule-based decision thresholds
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalSimulator(unittest.TestCase):
    def _bullish_smc_levels(self, score=90):
        return {
            "key_levels": [
                {"type": "Order Block Bullish", "price": 95, "low": 94, "high": 96, "tf": "1H", "strength": 6},
                {"type": "Equal Highs", "price": 120, "low": 119, "high": 121, "tf": "1H", "strength": 4},
            ],
            "confluence": {
                "score": score,
                "factors": {"structure_1D": "bullish", "structure_1H": "bullish"},
                "conflicts": [],
            },
        }

    def test_trade_signal_meets_minimum_rr_and_confidence(self):
        sig = signal_simulator.rule_based_signal(self._bullish_smc_levels(score=90), current_price=100)
        self.assertEqual(sig["decision"], "TRADE")
        self.assertEqual(sig["direction"], "LONG")
        self.assertGreaterEqual(sig["rr_ratio"], signal_simulator.MIN_RR - 0.01)
        self.assertGreaterEqual(sig["confidence"], signal_simulator.MIN_CONFIDENCE)
        self.assertLess(sig["stop_loss"], sig["entry_price"])
        self.assertGreater(sig["tp1"], sig["entry_price"])

    def test_below_confidence_threshold_is_no_trade(self):
        sig = signal_simulator.rule_based_signal(self._bullish_smc_levels(score=50), current_price=100)
        self.assertEqual(sig["decision"], "NO_TRADE")
        self.assertIsNone(sig["direction"])

    def test_neutral_bias_is_no_trade(self):
        levels = self._bullish_smc_levels(score=95)
        levels["confluence"]["factors"] = {"structure_1D": "neutral"}
        sig = signal_simulator.rule_based_signal(levels, current_price=100)
        self.assertEqual(sig["decision"], "NO_TRADE")

    def test_conflict_present_is_no_trade(self):
        levels = self._bullish_smc_levels(score=95)
        levels["confluence"]["conflicts"] = ["4H structure (bearish) conflicts with primary bias (bullish)"]
        sig = signal_simulator.rule_based_signal(levels, current_price=100)
        self.assertEqual(sig["decision"], "NO_TRADE")
        self.assertIn("conflict", sig["no_trade_reason"].lower())

    def test_no_zone_in_direction_is_no_trade(self):
        levels = self._bullish_smc_levels(score=95)
        levels["key_levels"] = []  # no OB/FVG at all
        sig = signal_simulator.rule_based_signal(levels, current_price=100)
        self.assertEqual(sig["decision"], "NO_TRADE")

    def test_should_sample_respects_zero_pct(self):
        results = [signal_simulator.should_sample(0.0) for _ in range(50)]
        self.assertFalse(any(results))

    def test_should_sample_respects_full_pct(self):
        results = [signal_simulator.should_sample(1.0) for _ in range(50)]
        self.assertTrue(all(results))


# ─────────────────────────────────────────────────────────────────────────────
# engine — vectorbt portfolio simulation
# ─────────────────────────────────────────────────────────────────────────────

class TestEngineSimulation(unittest.TestCase):
    def test_long_trade_hits_take_profit(self):
        # Flat at 100, then a clean ramp up through TP so a LONG entered at bar 10 wins.
        n = 60
        candles = _flat_series(n, price=100.0)
        for i in range(10, n):
            candles[i]["close"] = 100.0 + (i - 9) * 2.0  # ramps to well past TP
            candles[i]["high"] = candles[i]["close"] + 1
            candles[i]["low"] = candles[i]["close"] - 1

        signals = [{
            "bar_index": 10, "timestamp": candles[10]["timestamp"],
            "direction": "LONG", "entry_price": 100.0, "stop_loss": 95.0,
            "tp1": 115.0, "tp2": 120.0, "confidence": 90, "rr_ratio": 3.0,
        }]
        result = engine._simulate_portfolio(candles, signals, init_cash=1000.0, fees_pct=0.0004, slippage_pct=0.0005)
        self.assertEqual(result["total_trades"], 1)
        self.assertGreater(result["final_equity"], 1000.0)
        self.assertEqual(result["trades"][0]["direction"], "LONG")

    def test_short_trade_hits_stop_loss(self):
        n = 60
        candles = _flat_series(n, price=100.0)
        for i in range(10, n):
            candles[i]["close"] = 100.0 + (i - 9) * 2.0  # price rises against a SHORT
            candles[i]["high"] = candles[i]["close"] + 1
            candles[i]["low"] = candles[i]["close"] - 1

        signals = [{
            "bar_index": 10, "timestamp": candles[10]["timestamp"],
            "direction": "SHORT", "entry_price": 100.0, "stop_loss": 105.0,
            "tp1": 85.0, "tp2": 80.0, "confidence": 90, "rr_ratio": 3.0,
        }]
        result = engine._simulate_portfolio(candles, signals, init_cash=1000.0, fees_pct=0.0004, slippage_pct=0.0005)
        self.assertEqual(result["total_trades"], 1)
        self.assertLess(result["final_equity"], 1000.0)
        self.assertEqual(result["trades"][0]["direction"], "SHORT")

    def test_no_signals_returns_empty_result_shape(self):
        candles = _flat_series(30)
        result = engine._empty_result("BTC/USDT", datetime.now(timezone.utc), datetime.now(timezone.utc), len(candles), [])
        self.assertEqual(result["total_trades"], 0)
        self.assertEqual(result["signals_generated"], 0)
        self.assertIn("note", result)

    def test_stats_are_json_serializable(self):
        import json
        n = 60
        candles = _flat_series(n, price=100.0)
        for i in range(10, n):
            candles[i]["close"] = 100.0 + (i - 9) * 2.0
        signals = [{
            "bar_index": 10, "timestamp": candles[10]["timestamp"],
            "direction": "LONG", "entry_price": 100.0, "stop_loss": 95.0,
            "tp1": 115.0, "tp2": 120.0, "confidence": 90, "rr_ratio": 3.0,
        }]
        result = engine._simulate_portfolio(candles, signals, init_cash=1000.0, fees_pct=0.0004, slippage_pct=0.0005)
        json.dumps(result)  # raises if inf/NaN leaked through


# ─────────────────────────────────────────────────────────────────────────────
# data_loader — pagination + CandleCache, ccxt mocked (no network)
# ─────────────────────────────────────────────────────────────────────────────

class TestDataLoader(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        db.query(CandleCache).delete()
        db.commit()
        db.close()

    def _mock_exchange(self, pages):
        """pages: list of raw ccxt OHLCV batches to return on successive calls."""
        mock_ex = MagicMock()
        mock_ex.rateLimit = 0
        mock_ex.fetch_ohlcv.side_effect = pages
        return mock_ex

    def test_pagination_advances_cursor_and_stops_at_short_page(self):
        base = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        step = data_loader._TF_MS["1h"]
        page1 = [[base + i * step, 1, 2, 0.5, 1.5, 10] for i in range(3)]  # short page => stop
        mock_ex = self._mock_exchange([page1])

        with patch.object(data_loader, "_get_exchange", return_value=mock_ex):
            candles = data_loader.fetch_historical_ohlcv(
                "BTC/USDT", "1h",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                use_cache=False,
            )
        self.assertEqual(len(candles), 3)
        self.assertEqual(mock_ex.fetch_ohlcv.call_count, 1)

    def test_cache_roundtrip_avoids_second_exchange_call(self):
        base = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        step = data_loader._TF_MS["1h"]
        page1 = [[base + i * step, 1, 2, 0.5, 1.5, 10] for i in range(24)]
        mock_ex = self._mock_exchange([page1])

        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        until = datetime(2026, 1, 2, tzinfo=timezone.utc)

        with patch.object(data_loader, "_get_exchange", return_value=mock_ex):
            first = data_loader.fetch_historical_ohlcv("BTC/USDT", "1h", since, until, use_cache=True)
            second = data_loader.fetch_historical_ohlcv("BTC/USDT", "1h", since, until, use_cache=True)

        self.assertEqual(len(first), 24)
        self.assertEqual(len(second), 24)
        self.assertEqual(mock_ex.fetch_ohlcv.call_count, 1)  # second call served from cache

    def test_unsupported_timeframe_raises(self):
        with self.assertRaises(ValueError):
            data_loader.fetch_historical_ohlcv(
                "BTC/USDT", "3m",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
