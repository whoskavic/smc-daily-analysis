"""
Unit tests — executor.calculate_position_size's margin-at-balance cap, and
the paper/live execute_signal paths that consume it (no real network/API
calls; executor.py has no module-level app.config dependency, so no config
stub is needed for calculate_position_size itself).

Why: calculate_position_size previously derived quantity from risk alone,
with no check that the resulting margin fits the available balance. In the
2-year BTC/USDT backtest baseline, 43/134 trades needed more margin than
equity, and those trades had expectancy -3.65R vs -0.51R for the rest — an
unbounded quantity appears exactly when the stop is very tight. This
mirrors trade_simulator._size_position()'s cap-at-equity semantics exactly,
and additionally refuses (raises ValueError) a trade whose capped size
still falls below the exchange's minimum order size, instead of forcing an
unfillable order.
"""
import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

# ── Stub config (no .env needed) — mirrors the other test_*.py files.
# executor.py itself has no module-level app.config import, but the
# lazily-imported paper_wallet/factory modules end up importing it too, so
# stub it the same way as everywhere else for consistency. ──────────────
if "app" not in sys.modules:
    app_mod = types.ModuleType("app")
    app_mod.__path__ = [os.path.join(BACKEND, "app")]
    sys.modules["app"] = app_mod

if "app.config" not in sys.modules:
    cfg_mod = types.ModuleType("app.config")
    cfg_mod.settings = MagicMock()
    sys.modules["app.config"] = cfg_mod

# Set unconditionally (not gated on module creation above) — another
# test module discovered earlier may have already created app.config with
# its own MagicMock settings stub that doesn't set these attributes, and
# a bare MagicMock attribute isn't the concrete value these tests need.
_settings = sys.modules["app.config"].settings
_settings.binance_api_key = ""
_settings.binance_api_secret = ""
_settings.max_concurrent_positions = 3
_settings.trade_mode = "paper"
_settings.risk_per_trade_pct = 1.0

from app.services.exchange import executor  # noqa: E402
from app.services.exchange.executor import calculate_position_size, PositionSizeResult  # noqa: E402


class _FakeExchangeClient:
    """Async-context-manager stand-in for factory.ExchangeClient."""
    def __init__(self, exchange):
        self.exchange = exchange

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def _fake_meta(min_notional=1.0):
    return MagicMock(min_notional=min_notional)


def _base_signal(entry=100.0, stop=95.0, tp1=105.0, tp2=110.0, leverage=10):
    return {
        "decision": "TRADE", "direction": "LONG", "entry_price": entry,
        "stop_loss": stop, "tp1": tp1, "tp2": tp2,
        "tp1_close_pct": 50, "recommended_leverage": leverage,
        "symbol": "BTC/USDT",
    }


# ─────────────────────────────────────────────────────────────────────────────
# calculate_position_size — pure function, hand-computed numbers
# ─────────────────────────────────────────────────────────────────────────────

class TestCalculatePositionSize(unittest.TestCase):
    def test_normal_case_margin_below_balance_unchanged_no_cap(self):
        # risk_usdt=1000*0.01=10; sl_dist_pct=1/100=0.01; margin=10/(0.01*10)=100
        result = calculate_position_size(
            balance_usdt=1000.0, entry_price=100.0, stop_loss=99.0,
            risk_pct=0.01, leverage=10, min_notional=1.0,
        )
        self.assertEqual(result.margin_usdt, 100.0)
        self.assertEqual(result.quantity, 10.0)
        self.assertFalse(result.margin_capped)
        self.assertFalse(result.below_minimum)

    def test_tight_stop_required_margin_above_balance_caps_exactly(self):
        # risk_usdt=1000*0.01=10; sl_dist_pct=0.05/100=0.0005;
        # uncapped margin=10/(0.0005*10)=2000 > balance(1000) -> capped to 1000
        result = calculate_position_size(
            balance_usdt=1000.0, entry_price=100.0, stop_loss=99.95,
            risk_pct=0.01, leverage=10, min_notional=1.0,
        )
        self.assertEqual(result.margin_usdt, 1000.0)  # == balance exactly
        self.assertEqual(result.quantity, 100.0)       # 1000*10/100
        self.assertTrue(result.margin_capped)
        self.assertFalse(result.below_minimum)

    def test_capped_quantity_below_contract_minimum_is_flagged(self):
        # tiny balance + tight stop + full risk -> margin capped to the
        # tiny balance, and the resulting notional still can't clear
        # min_notional.
        result = calculate_position_size(
            balance_usdt=0.5, entry_price=100.0, stop_loss=99.9,
            risk_pct=1.0, leverage=1, min_notional=1.0,
        )
        self.assertTrue(result.margin_capped)
        self.assertEqual(result.margin_usdt, 0.5)
        self.assertLess(result.margin_usdt * 1, 1.0)  # notional < min_notional
        self.assertTrue(result.below_minimum)

    def test_below_minimum_without_capping_wide_stop_small_risk(self):
        # Not capped (margin well under balance) but still below the
        # exchange's minimum notional.
        result = calculate_position_size(
            balance_usdt=10.0, entry_price=100.0, stop_loss=50.0,
            risk_pct=0.01, leverage=1, min_notional=100.0,
        )
        self.assertFalse(result.margin_capped)
        self.assertTrue(result.below_minimum)

    def test_leverage_is_respected_in_margin_computation(self):
        low_lev = calculate_position_size(1000.0, 100.0, 99.0, 0.01, leverage=5, min_notional=1.0)
        high_lev = calculate_position_size(1000.0, 100.0, 99.0, 0.01, leverage=10, min_notional=1.0)
        # margin = risk_usdt / (sl_dist_pct * leverage) -- inversely proportional
        self.assertAlmostEqual(low_lev.margin_usdt, high_lev.margin_usdt * 2, places=4)
        self.assertFalse(low_lev.margin_capped)
        self.assertFalse(high_lev.margin_capped)

    def test_zero_stop_distance_falls_back_instead_of_dividing_by_zero(self):
        result = calculate_position_size(1000.0, 100.0, 100.0, 0.01, leverage=10, min_notional=1.0)
        self.assertGreater(result.margin_usdt, 0.0)

    def test_returns_position_size_result_dataclass(self):
        result = calculate_position_size(1000.0, 100.0, 99.0, 0.01, 10, 1.0)
        self.assertIsInstance(result, PositionSizeResult)


# ─────────────────────────────────────────────────────────────────────────────
# Paper path — execute_signal(mode="paper")
# ─────────────────────────────────────────────────────────────────────────────

class TestPaperPathMarginCap(unittest.TestCase):
    # entry_price is set in _base_signal (LIMIT signal), so since the
    # pending-orders PR the paper branch calls paper_place_pending_order,
    # not paper_open_position, for these signals — mock/assert accordingly.

    def test_capped_result_is_recorded_and_wallet_gets_full_available_balance(self):
        mock_place = AsyncMock(return_value={"order_id": "abc123", "status": "pending"})

        with patch("app.services.exchange.paper_wallet.has_position", return_value=False), \
             patch("app.services.exchange.paper_wallet.has_pending_order", return_value=False), \
             patch("app.services.exchange.paper_wallet.get_position_count", return_value=0), \
             patch("app.services.exchange.paper_wallet.get_wallet_state",
                   return_value={"available_usdt": 1000.0}), \
             patch("app.services.exchange.paper_wallet.paper_place_pending_order", mock_place), \
             patch("app.services.exchange.factory.get_meta", return_value=_fake_meta(1.0)), \
             patch.object(executor, "_get_trade_mode", return_value="paper"), \
             patch.object(executor, "_get_risk_pct", return_value=1.0):  # 100% risk -> forces a cap
            signal = _base_signal(entry=100.0, stop=99.95, leverage=10)
            result = asyncio.run(executor.execute_signal(signal, "bybit"))

        self.assertEqual(result["mode"], "paper")
        self.assertTrue(result["margin_capped"])
        mock_place.assert_awaited_once()
        _, kwargs = mock_place.call_args
        self.assertAlmostEqual(kwargs["margin_usdt"], 1000.0)  # capped to the full available balance

    def test_below_minimum_raises_instead_of_opening_a_position(self):
        mock_place = AsyncMock()

        with patch("app.services.exchange.paper_wallet.has_position", return_value=False), \
             patch("app.services.exchange.paper_wallet.has_pending_order", return_value=False), \
             patch("app.services.exchange.paper_wallet.get_position_count", return_value=0), \
             patch("app.services.exchange.paper_wallet.get_wallet_state",
                   return_value={"available_usdt": 0.5}), \
             patch("app.services.exchange.paper_wallet.paper_place_pending_order", mock_place), \
             patch("app.services.exchange.factory.get_meta", return_value=_fake_meta(1.0)), \
             patch.object(executor, "_get_trade_mode", return_value="paper"), \
             patch.object(executor, "_get_risk_pct", return_value=1.0):
            signal = _base_signal(entry=100.0, stop=99.9, leverage=1)
            with self.assertRaises(ValueError):
                asyncio.run(executor.execute_signal(signal, "bybit"))

        mock_place.assert_not_awaited()

    def test_normal_case_margin_unchanged_no_cap(self):
        mock_place = AsyncMock(return_value={"order_id": "xyz789", "status": "pending"})

        with patch("app.services.exchange.paper_wallet.has_position", return_value=False), \
             patch("app.services.exchange.paper_wallet.has_pending_order", return_value=False), \
             patch("app.services.exchange.paper_wallet.get_position_count", return_value=0), \
             patch("app.services.exchange.paper_wallet.get_wallet_state",
                   return_value={"available_usdt": 1000.0}), \
             patch("app.services.exchange.paper_wallet.paper_place_pending_order", mock_place), \
             patch("app.services.exchange.factory.get_meta", return_value=_fake_meta(1.0)), \
             patch.object(executor, "_get_trade_mode", return_value="paper"), \
             patch.object(executor, "_get_risk_pct", return_value=0.01):
            signal = _base_signal(entry=100.0, stop=99.0, leverage=10)
            result = asyncio.run(executor.execute_signal(signal, "bybit"))

        self.assertFalse(result["margin_capped"])
        _, kwargs = mock_place.call_args
        self.assertAlmostEqual(kwargs["margin_usdt"], 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# Live path — execute_signal(mode="live")
# ─────────────────────────────────────────────────────────────────────────────

class TestLivePathMarginCap(unittest.TestCase):
    def _fake_exchange(self):
        ex = MagicMock()
        ex.set_leverage = AsyncMock()
        ex.create_order = AsyncMock(return_value={"id": "order123"})
        ex.fetch_ticker = AsyncMock(return_value={"last": 100.0})
        return ex

    def test_capped_result_is_recorded_in_live_result(self):
        fake_ex = self._fake_exchange()
        with patch("app.services.exchange.factory.create_exchange",
                   return_value=_FakeExchangeClient(fake_ex)), \
             patch("app.services.exchange.factory.get_meta", return_value=_fake_meta(1.0)), \
             patch.object(executor, "_get_trade_mode", return_value="live"), \
             patch.object(executor, "_get_risk_pct", return_value=1.0):  # 100% risk -> forces a cap
            signal = _base_signal(entry=100.0, stop=99.95, leverage=10)
            result = asyncio.run(executor.execute_signal(signal, "bybit", balance_usdt=1000.0))

        self.assertEqual(result["mode"], "live")
        self.assertTrue(result["margin_capped"])
        self.assertEqual(result["margin_usdt"], 1000.0)
        self.assertEqual(result["size"], 100.0)

    def test_below_minimum_raises_instead_of_placing_orders(self):
        fake_ex = self._fake_exchange()
        with patch("app.services.exchange.factory.create_exchange",
                   return_value=_FakeExchangeClient(fake_ex)), \
             patch("app.services.exchange.factory.get_meta", return_value=_fake_meta(1.0)), \
             patch.object(executor, "_get_trade_mode", return_value="live"), \
             patch.object(executor, "_get_risk_pct", return_value=1.0):
            signal = _base_signal(entry=100.0, stop=99.9, leverage=1)
            with self.assertRaises(ValueError):
                asyncio.run(executor.execute_signal(signal, "bybit", balance_usdt=0.5))

        fake_ex.create_order.assert_not_awaited()

    def test_normal_case_no_cap_in_live_result(self):
        fake_ex = self._fake_exchange()
        with patch("app.services.exchange.factory.create_exchange",
                   return_value=_FakeExchangeClient(fake_ex)), \
             patch("app.services.exchange.factory.get_meta", return_value=_fake_meta(1.0)), \
             patch.object(executor, "_get_trade_mode", return_value="live"), \
             patch.object(executor, "_get_risk_pct", return_value=0.01):
            signal = _base_signal(entry=100.0, stop=99.0, leverage=10)
            result = asyncio.run(executor.execute_signal(signal, "bybit", balance_usdt=1000.0))

        self.assertFalse(result["margin_capped"])
        self.assertEqual(result["margin_usdt"], 100.0)
        self.assertEqual(result["size"], 10.0)


if __name__ == "__main__":
    unittest.main()
