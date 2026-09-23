"""
Unit tests — paper wallet pending LIMIT orders (fills + cancellation rules).

Why: paper_open_position previously filled a LIMIT signal instantly at
entry_price, with no pending-order stage, so paper reported a 100% fill
rate at prices the market may never have traded. trade_simulator.py (the
validated 2-year backtest reference) fills LIMIT orders only when price
trades through entry, and cancels pending orders on TTL / TP1-before-fill /
bias-flip. This mirrors that state machine for paper's single polled mark
price (no OHLC bar) — see paper_wallet.py's module docstring and the
_try_limit_fill/_reached_tp1 docstrings for the exact adaptation.

bias_flip is NOT implemented here (see paper_update_pending_orders'
docstring and the PR report): the live/paper poll cadence (scheduler's 60s
update_paper_positions job) has no lightweight current-bias source — the
only bias comes from the separately-scheduled, expensive Claude-based
daily/swarm analysis jobs, and inventing a new per-tick SMC recomputation
was explicitly out of scope for this PR.
"""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

# ── Stub config (no .env needed) — mirrors test_phase4.py / test_executor_margin_cap.py.
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()

if "app" not in sys.modules:
    app_mod = types.ModuleType("app")
    app_mod.__path__ = [os.path.join(BACKEND, "app")]
    sys.modules["app"] = app_mod

if "app.config" not in sys.modules:
    cfg_mod = types.ModuleType("app.config")
    cfg_mod.settings = MagicMock()
    sys.modules["app.config"] = cfg_mod

_settings = sys.modules["app.config"].settings
_settings.anthropic_api_key = "sk-test-stub"
_settings.binance_api_key = ""
_settings.binance_api_secret = ""
_settings.database_url = f"sqlite:///{_TMP_DB.name}"
_settings.max_concurrent_positions = 3
_settings.trade_mode = "paper"
_settings.active_exchange = "binance"
_settings.risk_per_trade_pct = 1.0
_settings.paper_order_ttl_minutes = 240
_settings.is_paper_mode = lambda: True
_settings.is_live_mode = lambda: False

from app.models.database import init_db  # noqa: E402
from app.services.exchange import paper_wallet  # noqa: E402
from app.services.exchange import executor  # noqa: E402
from app.services import scheduler  # noqa: E402

init_db()


class _FakeTicker:
    def __init__(self, prices: dict):
        self._prices = prices

    async def fetch_ticker(self, symbol):
        return {"last": self._prices.get(symbol, 0.0)}


class _FakeExchangeClient:
    """Async-context-manager stand-in for factory.ExchangeClient."""
    def __init__(self, prices: dict):
        self.exchange = _FakeTicker(prices)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def _patch_prices(prices: dict):
    """Patch app.services.exchange.factory.create_exchange (used by both
    paper_update_pending_orders and paper_update_positions) to return a
    fixed mark price per symbol."""
    return patch(
        "app.services.exchange.factory.create_exchange",
        return_value=_FakeExchangeClient(prices),
    )


def _order_kwargs(**overrides):
    kwargs = dict(
        symbol="BTC/USDT", direction="LONG", entry_price=100.0, stop_loss=95.0,
        tp1=110.0, tp2=120.0, margin_usdt=50.0, quantity=5.0, leverage=10,
        tp1_pct=0.50, exchange_id="binance",
    )
    kwargs.update(overrides)
    return kwargs


class PaperPendingOrdersTestBase(unittest.TestCase):
    def setUp(self):
        paper_wallet.reset_wallet(1000.0)

    def tearDown(self):
        paper_wallet.reset_wallet(1000.0)


# ─────────────────────────────────────────────────────────────────────────────
# 1. LIMIT signal creates a pending order and NO position; available unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingOrderCreation(PaperPendingOrdersTestBase):
    def test_limit_signal_creates_pending_order_no_position_available_unchanged(self):
        before = paper_wallet.get_wallet_state()["available_usdt"]
        order = asyncio.run(paper_wallet.paper_place_pending_order(**_order_kwargs()))

        self.assertEqual(order["status"], "pending")
        self.assertEqual(len(paper_wallet.get_pending_orders()), 1)
        self.assertEqual(len(paper_wallet.get_open_positions()), 0)
        self.assertEqual(paper_wallet.get_wallet_state()["available_usdt"], before)
        self.assertTrue(paper_wallet.has_pending_order("BTC/USDT"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. Price trading through entry fills it — matches today's shape, margin
#    committed exactly once
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingOrderFill(PaperPendingOrdersTestBase):
    def test_price_through_entry_fills_matches_shape_margin_committed_once(self):
        asyncio.run(paper_wallet.paper_place_pending_order(**_order_kwargs()))
        before = paper_wallet.get_wallet_state()["available_usdt"]

        with _patch_prices({"BTC/USDT": 99.0}):  # <= entry(100) for LONG -> fills
            events = asyncio.run(paper_wallet.paper_update_pending_orders())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "filled")

        self.assertEqual(len(paper_wallet.get_pending_orders()), 0)
        positions = paper_wallet.get_open_positions()
        self.assertEqual(len(positions), 1)
        pos = positions[0]

        # Same record shape as paper_open_position's, plus order lifecycle fields.
        expected_keys = {
            "trade_id", "symbol", "direction", "order_type", "size", "entry_price",
            "stop_loss", "tp1", "tp2", "tp1_pct", "tp2_pct", "tp1_hit", "tp2_hit",
            "leverage", "margin_usdt", "notional_usdt", "rr_tp1", "rr_tp2",
            "unrealized_pnl", "realized_pnl", "status", "opened_at", "exchange_id",
            "order_id", "order_created_at", "fill_time",
        }
        self.assertEqual(expected_keys, set(pos.keys()))
        self.assertEqual(pos["order_type"], "LIMIT")
        self.assertEqual(pos["entry_price"], 99.0)
        self.assertEqual(pos["status"], "open")

        after = paper_wallet.get_wallet_state()["available_usdt"]
        self.assertAlmostEqual(before - after, 50.0)  # margin committed exactly once

        # Sized on the ORDER's entry_price, never recomputed from the fill price.
        self.assertEqual(pos["size"], 5.0)
        self.assertEqual(pos["margin_usdt"], 50.0)

        # A second poll (nothing pending) must not touch the wallet again.
        with _patch_prices({"BTC/USDT": 99.0}):
            events2 = asyncio.run(paper_wallet.paper_update_pending_orders())
        self.assertEqual(events2, [])
        self.assertEqual(paper_wallet.get_wallet_state()["available_usdt"], after)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Price never reaching entry leaves it pending
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingOrderStaysOpen(PaperPendingOrdersTestBase):
    def test_price_never_reaching_entry_leaves_it_pending(self):
        asyncio.run(paper_wallet.paper_place_pending_order(**_order_kwargs()))

        # LONG entry=100, tp1=110 -> 105 neither fills (not <=100) nor hits TP1.
        with _patch_prices({"BTC/USDT": 105.0}):
            events = asyncio.run(paper_wallet.paper_update_pending_orders())

        self.assertEqual(events, [])
        self.assertEqual(len(paper_wallet.get_pending_orders()), 1)
        self.assertEqual(len(paper_wallet.get_open_positions()), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. TTL expiry cancels it, wallet untouched
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingOrderTTL(PaperPendingOrdersTestBase):
    def test_ttl_expiry_cancels_it_wallet_untouched(self):
        order = asyncio.run(paper_wallet.paper_place_pending_order(**_order_kwargs()))
        before = paper_wallet.get_wallet_state()["available_usdt"]

        # Backdate the order past the 240-minute TTL.
        order_id = order["order_id"]
        paper_wallet._pending_orders[order_id]["created_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=241)
        )

        # Price that neither fills nor hits TP1.
        with _patch_prices({"BTC/USDT": 105.0}):
            events = asyncio.run(paper_wallet.paper_update_pending_orders())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "cancelled")
        self.assertEqual(events[0]["reason"], "ttl")
        self.assertEqual(len(paper_wallet.get_pending_orders()), 0)
        self.assertEqual(len(paper_wallet.get_open_positions()), 0)
        self.assertEqual(paper_wallet.get_wallet_state()["available_usdt"], before)


# ─────────────────────────────────────────────────────────────────────────────
# 5. TP1 reached before fill cancels it
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingOrderTP1BeforeFill(PaperPendingOrdersTestBase):
    def test_tp1_reached_before_fill_cancels_it(self):
        # entry far below current price so it can't fill; tp1 within reach.
        kwargs = _order_kwargs(entry_price=90.0, tp1=110.0)
        asyncio.run(paper_wallet.paper_place_pending_order(**kwargs))
        before = paper_wallet.get_wallet_state()["available_usdt"]

        with _patch_prices({"BTC/USDT": 115.0}):  # >= tp1(110), not <= entry(90)
            events = asyncio.run(paper_wallet.paper_update_pending_orders())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "cancelled")
        self.assertEqual(events[0]["reason"], "tp1_before_fill")
        self.assertEqual(len(paper_wallet.get_pending_orders()), 0)
        self.assertEqual(paper_wallet.get_wallet_state()["available_usdt"], before)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Identical setup while one is pending is refused
# ─────────────────────────────────────────────────────────────────────────────

def _base_signal(entry=100.0, stop=95.0, tp1=110.0, tp2=120.0, leverage=10, symbol="BTC/USDT"):
    return {
        "decision": "TRADE", "direction": "LONG", "entry_price": entry,
        "stop_loss": stop, "tp1": tp1, "tp2": tp2,
        "tp1_close_pct": 50, "recommended_leverage": leverage,
        "symbol": symbol,
    }


def _fake_meta(min_notional=1.0):
    return MagicMock(min_notional=min_notional)


class TestNoReentryWhilePending(PaperPendingOrdersTestBase):
    def test_identical_setup_while_pending_is_refused(self):
        with patch("app.services.exchange.factory.get_meta", return_value=_fake_meta(1.0)), \
             patch.object(executor, "_get_trade_mode", return_value="paper"), \
             patch.object(executor, "_get_risk_pct", return_value=0.01):
            signal = _base_signal()
            result1 = asyncio.run(executor.execute_signal(signal, "binance"))
            self.assertEqual(result1["status"], "pending")
            self.assertEqual(paper_wallet.get_pending_order_count(), 1)

            with self.assertRaises(ValueError):
                asyncio.run(executor.execute_signal(signal, "binance"))

        # Still exactly one pending order — the second call was refused, not queued.
        self.assertEqual(paper_wallet.get_pending_order_count(), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 7. MARKET signal still fills immediately (regression guard)
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketSignalRegression(PaperPendingOrdersTestBase):
    def test_market_signal_still_fills_immediately(self):
        signal = _base_signal(entry=None)

        with patch("app.services.exchange.factory.get_meta", return_value=_fake_meta(1.0)), \
             patch.object(executor, "_get_trade_mode", return_value="paper"), \
             patch.object(executor, "_get_risk_pct", return_value=0.01), \
             patch.object(paper_wallet, "_get_mark_price", AsyncMock(return_value=100.0)):
            result = asyncio.run(executor.execute_signal(signal, "binance"))

        self.assertEqual(result["status"], "open")
        self.assertEqual(paper_wallet.get_pending_order_count(), 0)
        self.assertEqual(len(paper_wallet.get_open_positions()), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 8. A filled pending order then behaves exactly as a position does today —
#    SL/TP1/TP2 all still work through paper_update_positions
# ─────────────────────────────────────────────────────────────────────────────

class TestFilledOrderBehavesAsPosition(PaperPendingOrdersTestBase):
    def test_filled_order_then_sl_tp1_tp2_work_via_paper_update_positions(self):
        kwargs = _order_kwargs(entry_price=100.0, stop_loss=95.0, tp1=110.0, tp2=120.0)
        asyncio.run(paper_wallet.paper_place_pending_order(**kwargs))

        with _patch_prices({"BTC/USDT": 99.0}):  # fills the LONG
            asyncio.run(paper_wallet.paper_update_pending_orders())

        self.assertEqual(len(paper_wallet.get_open_positions()), 1)

        # TP1 hit -> partial close, SL moves to breakeven.
        with _patch_prices({"BTC/USDT": 110.0}):
            closed = asyncio.run(paper_wallet.paper_update_positions())
        self.assertEqual(closed, [])  # TP1 is a partial close, not a full close
        pos = paper_wallet.get_open_positions()[0]
        self.assertTrue(pos["tp1_hit"])
        self.assertEqual(pos["stop_loss"], 99.0)  # moved to fill entry (breakeven)

        # TP2 hit -> full close.
        with _patch_prices({"BTC/USDT": 120.0}):
            closed = asyncio.run(paper_wallet.paper_update_positions())
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["close_reason"], "TP2")
        self.assertEqual(len(paper_wallet.get_open_positions()), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 9. The scheduler job advances pending orders before positions
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerOrdering(unittest.TestCase):
    def test_scheduler_advances_pending_orders_before_positions(self):
        call_order = []

        async def fake_pending(*a, **kw):
            call_order.append("pending")
            return []

        async def fake_positions(*a, **kw):
            call_order.append("positions")
            return []

        with patch.object(scheduler.settings, "is_paper_mode", return_value=True), \
             patch("app.services.exchange.paper_wallet.get_pending_order_count", return_value=1), \
             patch("app.services.exchange.paper_wallet.get_position_count", return_value=1), \
             patch("app.services.exchange.paper_wallet.paper_update_pending_orders", fake_pending), \
             patch("app.services.exchange.paper_wallet.paper_update_positions", fake_positions):
            asyncio.run(scheduler.update_paper_positions())

        self.assertEqual(call_order, ["pending", "positions"])


# ─────────────────────────────────────────────────────────────────────────────
# 10. A fill and a cancel each broadcast the expected ws event
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerWsBroadcast(PaperPendingOrdersTestBase):
    def test_fill_broadcasts_order_filled_event(self):
        asyncio.run(paper_wallet.paper_place_pending_order(**_order_kwargs()))

        with patch.object(scheduler.settings, "is_paper_mode", return_value=True), \
             patch.object(scheduler.ws_manager, "broadcast_nowait") as mock_broadcast, \
             _patch_prices({"BTC/USDT": 99.0}):
            asyncio.run(scheduler.update_paper_positions())

        events = [c.args[1] for c in mock_broadcast.call_args_list if c.args[0] == "trade_update"]
        filled = [e for e in events if e["event"] == "order_filled"]
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["symbol"], "BTC/USDT")
        self.assertEqual(filled[0]["direction"], "LONG")
        self.assertEqual(filled[0]["mode"], "paper")

    def test_cancel_broadcasts_order_cancelled_event(self):
        kwargs = _order_kwargs(entry_price=90.0, tp1=110.0)
        asyncio.run(paper_wallet.paper_place_pending_order(**kwargs))

        with patch.object(scheduler.settings, "is_paper_mode", return_value=True), \
             patch.object(scheduler.ws_manager, "broadcast_nowait") as mock_broadcast, \
             _patch_prices({"BTC/USDT": 115.0}):  # tp1 reached before fill
            asyncio.run(scheduler.update_paper_positions())

        events = [c.args[1] for c in mock_broadcast.call_args_list if c.args[0] == "trade_update"]
        cancelled = [e for e in events if e["event"] == "order_cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["reason"], "tp1_before_fill")
        self.assertEqual(cancelled[0]["mode"], "paper")


if __name__ == "__main__":
    unittest.main()
