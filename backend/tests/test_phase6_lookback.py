"""
Unit tests Phase 6 — PR #13b: HTF lookback before `since` (backtest-only).

Covers: engine._fetch_with_lookback fetches each timeframe from
`since - lookback` and computes a dynamic warmup_bars so smc_replay's HTF
windows are already full at the first in-range bar; decisions/signals/
trades/equity-curve/metrics are scoped to [since, until) only even though
candles_15m/1h/4h/1d carry extra lookback history; and the no-earlier-data
fallback sets lookback_complete=False.
"""
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

# ── Stub config (no .env needed) — mirrors test_phase6_proxy.py ────────────
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

_settings = sys.modules["app.config"].settings
_settings.anthropic_api_key = "sk-test-stub"
_settings.claude_model = "claude-sonnet-4-6"
_settings.binance_api_key = ""
_settings.binance_api_secret = ""
_settings.database_url = f"sqlite:///{_TMP_DB.name}"
_settings.risk_per_trade_pct = 1.0
_settings.max_leverage = 10
_settings.timezone = "Asia/Jakarta"
_settings.daily_analysis_time = "08:00"

from app.models.database import init_db  # noqa: E402
from app.services.backtest import engine, smc_replay  # noqa: E402

init_db()


def C(ts, o, h, l, c, v=100.0):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


REF = datetime(2026, 1, 1, tzinfo=timezone.utc)  # midnight UTC — aligned to 1D/4H/1H/15m boundaries


def _series(step_minutes: int, start: datetime, total_days: int, price: float = 100.0):
    step = timedelta(minutes=step_minutes)
    n = int(timedelta(days=total_days) / step)
    return [C((start + i * step).isoformat(), price, price + 1, price - 1, price) for i in range(n)]


def _full_by_tf(total_days: int, start: datetime = REF, price: float = 100.0):
    return {
        "15m": _series(15, start, total_days, price),
        "1h": _series(60, start, total_days, price),
        "4h": _series(240, start, total_days, price),
        "1d": _series(1440, start, total_days, price),
    }


def _fetch_from(full_by_tf):
    """fetch_historical_ohlcv stand-in that actually respects `since`/`until`
    per timeframe (unlike the fixed return_value=candles mocks used
    elsewhere), so it exercises the real per-timeframe lookback fetch."""
    def _fetch(symbol, timeframe, since_arg, until_arg, use_cache=True):
        full = full_by_tf[timeframe]
        return [c for c in full if since_arg <= datetime.fromisoformat(c["timestamp"]) < until_arg]
    return _fetch


def _always_trade_signal(smc_levels, current_price, **kwargs):
    return {
        "decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
        "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
        "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_with_lookback — window fullness at the first in-range bar
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchWithLookback(unittest.TestCase):
    def test_htf_windows_are_full_at_first_in_range_bar(self):
        # 40 days of history; since is 32 days in (exactly covers the 1D
        # lookback margin: WINDOW_1D(30)+2 days), until is 8 days later.
        full = _full_by_tf(total_days=40)
        since = REF + timedelta(days=32)
        until = REF + timedelta(days=40)

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", side_effect=_fetch_from(full)):
            fetch = engine._fetch_with_lookback("BTC/USDT", since, until)

        self.assertTrue(fetch["lookback_complete"])
        self.assertLess(datetime.fromisoformat(fetch["lookback_start"]), since)

        snapshots = smc_replay.replay(
            fetch["candles_15m"], fetch["candles_1h"], fetch["candles_4h"], fetch["candles_1d"],
            warmup_bars=fetch["warmup_bars"],
        )
        first = next(snapshots)

        self.assertEqual(first["timestamp"], since.isoformat())
        self.assertEqual(len(first["candles_1d"]), smc_replay.WINDOW_1D)
        self.assertEqual(len(first["candles_4h"]), smc_replay.WINDOW_4H)
        self.assertEqual(len(first["candles_1h"]), smc_replay.WINDOW_1H)
        self.assertEqual(len(first["candles_15m"]), smc_replay.WINDOW_15M)

    def test_no_data_before_since_falls_back_and_marks_incomplete(self):
        # fetch_historical_ohlcv returns candles starting exactly at `since`
        # (as if the exchange/cache had nothing earlier) regardless of the
        # lookback-adjusted since it was actually asked for.
        since = REF
        until = REF + timedelta(days=2)
        candles = _series(15, since, 2)

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles):
            fetch = engine._fetch_with_lookback("BTC/USDT", since, until)

        self.assertFalse(fetch["lookback_complete"])
        self.assertEqual(fetch["warmup_bars"], min(smc_replay.DEFAULT_WARMUP_BARS, len(candles)))
        self.assertEqual(fetch["lookback_start"], since.isoformat())

    def test_empty_fetch_falls_back_and_marks_incomplete(self):
        since = REF
        until = REF + timedelta(days=2)

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=[]):
            fetch = engine._fetch_with_lookback("BTC/USDT", since, until)

        self.assertFalse(fetch["lookback_complete"])
        self.assertEqual(fetch["warmup_bars"], 0)

    def test_stale_1d_cache_triggers_refetch_and_recovers_completeness(self):
        """data_loader's cache accepts a >=95%-coverage hit — on a long
        backtest, a cache holding only the in-range portion (no real
        lookback) can pass that check and be returned as-is. Simulate that:
        the first (use_cache=True) call for 1D returns only [since, until),
        ignoring how far back it was actually asked to fetch; a fresh
        (use_cache=False) pull returns the real, fully-lookback-covered
        history."""
        full = _full_by_tf(total_days=40)
        since = REF + timedelta(days=32)
        until = REF + timedelta(days=40)

        def fetch(symbol, timeframe, since_arg, until_arg, use_cache=True):
            src = full[timeframe]
            lo = since if (timeframe == "1d" and use_cache) else since_arg
            return [c for c in src if lo <= datetime.fromisoformat(c["timestamp"]) < until_arg]

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", side_effect=fetch):
            result = engine._fetch_with_lookback("BTC/USDT", since, until)

        self.assertTrue(result["lookback_by_tf"]["1d"]["complete"])
        self.assertTrue(result["lookback_complete"])
        recovered_earliest = datetime.fromisoformat(result["lookback_by_tf"]["1d"]["earliest"])
        self.assertLess(recovered_earliest, since - timedelta(days=smc_replay.WINDOW_1D))

    def test_genuinely_missing_1d_history_marks_incomplete(self):
        """Unlike the stale-cache case, here NEITHER the cached nor the
        fresh (use_cache=False) pull for 1D reaches back far enough — the
        exchange genuinely lacks the history. lookback_by_tf["1d"] stays
        incomplete and drags the overall lookback_complete down, even
        though every other timeframe is fully covered."""
        full = _full_by_tf(total_days=40)
        since = REF + timedelta(days=32)
        until = REF + timedelta(days=40)

        def fetch(symbol, timeframe, since_arg, until_arg, use_cache=True):
            src = full[timeframe]
            lo = since if timeframe == "1d" else since_arg
            return [c for c in src if lo <= datetime.fromisoformat(c["timestamp"]) < until_arg]

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", side_effect=fetch):
            result = engine._fetch_with_lookback("BTC/USDT", since, until)

        self.assertFalse(result["lookback_by_tf"]["1d"]["complete"])
        self.assertFalse(result["lookback_complete"])
        # the other timeframes were genuinely fetched with full lookback and
        # aren't dragged down by 1D's shortfall individually
        self.assertTrue(result["lookback_by_tf"]["15m"]["complete"])
        self.assertTrue(result["lookback_by_tf"]["1h"]["complete"])
        self.assertTrue(result["lookback_by_tf"]["4h"]["complete"])


# ─────────────────────────────────────────────────────────────────────────────
# run_backtest — decisions/signals/trades/equity curve/metrics scoped to
# [since, until); lookback bars are context only
# ─────────────────────────────────────────────────────────────────────────────

class TestRunBacktestLookback(unittest.TestCase):
    def test_min_bars_required_counts_in_range_bars_only(self):
        """MIN_BARS_REQUIRED must gate on bars actually in [since, until) —
        not the lookback-inflated fetched array. Here the fetched 15m array
        (with a full ~32-day lookback) is ~292 bars, comfortably over
        MIN_BARS_REQUIRED(200), but the requested range itself is only 100
        bars — too few to backtest — and that must still raise."""
        full = _full_by_tf(total_days=40)
        since = REF + timedelta(days=32)
        until = since + timedelta(minutes=15 * 100)  # 100 in-range 15m bars only

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", side_effect=_fetch_from(full)):
            with self.assertRaises(ValueError) as ctx:
                engine.run_backtest("BTC/USDT", since=since, until=until)

        self.assertIn("100 bars", str(ctx.exception))
        self.assertIn("need >= 200", str(ctx.exception))

    def test_full_requested_range_is_decision_bars_when_lookback_available(self):
        """Before this fix, the first DEFAULT_WARMUP_BARS(120) bars of ANY
        requested range were burned as bar-count warmup — even 30 days in,
        losing ~30h of the caller's requested range. With lookback fetched
        from before `since`, every bar the caller asked for becomes a
        decision bar."""
        full = _full_by_tf(total_days=40)
        since = REF + timedelta(days=32)
        until = REF + timedelta(days=40)
        expected_bars = int((until - since) / timedelta(minutes=15))  # 8 days * 96

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", side_effect=_fetch_from(full)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=False):
            result = engine.run_backtest("BTC/USDT", since=since, until=until, decision_schedule="every_bar")

        self.assertTrue(result["lookback_complete"])
        self.assertEqual(result["bars_analyzed"], expected_bars)
        self.assertEqual(result["decision_bars"], expected_bars)
        self.assertEqual(result["since"], since.isoformat())
        self.assertEqual(result["until"], until.isoformat())
        self.assertLess(datetime.fromisoformat(result["lookback_start"]), since)

    def test_no_signal_order_or_trade_timestamp_before_since(self):
        full = _full_by_tf(total_days=40)
        since = REF + timedelta(days=32)
        until = REF + timedelta(days=40)

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", side_effect=_fetch_from(full)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=_always_trade_signal):
            result = engine.run_backtest("BTC/USDT", since=since, until=until, decision_schedule="every_bar")

        since_iso = since.isoformat()
        self.assertGreater(result["total_trades"], 0)
        for t in result["trades"]:
            self.assertGreaterEqual(t["signal_time"], since_iso)
            self.assertGreaterEqual(t["entry_time"], since_iso)
            self.assertGreaterEqual(t["exit_time"], since_iso)
        self.assertGreater(len(result["equity_curve"]), 0)
        for point in result["equity_curve"]:
            self.assertGreaterEqual(point["timestamp"], since_iso)

    def test_metrics_unchanged_when_lookback_pattern_matches_in_range(self):
        """Sanity: an identical, uniform candle pattern spanning both the
        lookback region and the in-range region must not perturb metrics —
        running with (available) lookback data vs. running where the
        exchange has no earlier data at all (fallback path) on the same
        in-range slice gives the same trade outcomes."""
        since = REF + timedelta(days=32)
        until = REF + timedelta(days=40)

        full_with_lookback = _full_by_tf(total_days=40)
        with patch.object(engine.data_loader, "fetch_historical_ohlcv", side_effect=_fetch_from(full_with_lookback)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=_always_trade_signal):
            with_lookback = engine.run_backtest("BTC/USDT", since=since, until=until, decision_schedule="every_bar")

        # Same in-range pattern, but the exchange only has data from `since`
        # onward (no lookback available at all) — the fallback path.
        in_range_only = {tf: [c for c in candles if since.isoformat() <= c["timestamp"]]
                          for tf, candles in full_with_lookback.items()}
        with patch.object(engine.data_loader, "fetch_historical_ohlcv", side_effect=_fetch_from(in_range_only)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=_always_trade_signal):
            fallback = engine.run_backtest("BTC/USDT", since=since, until=until, decision_schedule="every_bar")

        self.assertTrue(with_lookback["lookback_complete"])
        self.assertFalse(fallback["lookback_complete"])
        self.assertEqual(with_lookback["total_trades"], fallback["total_trades"])
        self.assertEqual(with_lookback["final_equity"], fallback["final_equity"])
        self.assertEqual(with_lookback["total_return_pct"], fallback["total_return_pct"])


if __name__ == "__main__":
    unittest.main()
