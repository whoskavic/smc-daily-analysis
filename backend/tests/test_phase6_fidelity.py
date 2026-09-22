"""
Unit tests Phase 6 — backtest fidelity (no real exchange/network calls, no
real Claude API calls). Mirrors the stubbing pattern used in test_phase4.py
so these run without a .env file.

Covers: smc_replay's no-lookahead HTF windowing (forming-candle rebuild vs.
the old timestamp-only filter), trade_simulator's event-driven order/position
lifecycle, and engine.run_backtest's sim_mode dispatch.
"""
import os
import random
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

_settings = sys.modules["app.config"].settings
_settings.anthropic_api_key = "sk-test-stub"
_settings.claude_model = "claude-sonnet-4-6"
_settings.binance_api_key = ""
_settings.binance_api_secret = ""
_settings.database_url = f"sqlite:///{_TMP_DB.name}"
_settings.risk_per_trade_pct = 1.0
_settings.max_leverage = 10

from app.models.database import init_db  # noqa: E402
from app.services.backtest import smc_replay, signal_simulator, engine, trade_simulator  # noqa: E402
from app.services.backtest.trade_simulator import SimConfig  # noqa: E402

init_db()


def C(ts, o, h, l, c, v=100.0):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _ts(i, step_minutes=15, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
    return (start + timedelta(minutes=i * step_minutes)).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# smc_replay — no-lookahead HTF windows
# ─────────────────────────────────────────────────────────────────────────────

class TestSmcReplayNoLookahead(unittest.TestCase):
    def _build_1h_with_future_leak(self, candles_15m, hour_start_idx):
        """A 'fully known' hourly series (as a real historical fetch would
        return) built by aggregating all 4 15m bars per hour — including
        bars AFTER `hour_start_idx`'s hour that haven't happened yet as of
        the bar we'll actually replay."""
        hourly = []
        for h in range(len(candles_15m) // 4):
            chunk = candles_15m[h * 4:(h + 1) * 4]
            hourly.append(C(
                chunk[0]["timestamp"], chunk[0]["open"],
                max(c["high"] for c in chunk), min(c["low"] for c in chunk),
                chunk[-1]["close"], sum(c["volume"] for c in chunk),
            ))
        return hourly

    def test_forming_htf_candle_excludes_future_extreme(self):
        # 400 bars of flat, boring 15m data...
        n = 400
        candles_15m = [C(_ts(i), 100.0, 100.5, 99.5, 100.0) for i in range(n)]
        # ...except one hour where the LAST TWO 15m bars (not yet seen as of
        # the bar we replay, which is the hour's 2nd bar) spike to a future-
        # only extreme high of 500. If lookahead leaked, the HTF candle for
        # that hour would show high=500 even though the replay bar is only
        # at the hour's 2nd 15m bar.
        target_hour_start = 200  # aligned to an hour boundary (200 % 4 == 0)
        candles_15m[target_hour_start + 2] = C(_ts(target_hour_start + 2), 100.0, 500.0, 99.5, 100.0)
        candles_15m[target_hour_start + 3] = C(_ts(target_hour_start + 3), 100.0, 500.0, 99.5, 100.0)

        candles_1h = self._build_1h_with_future_leak(candles_15m, target_hour_start)
        candles_4h = candles_1h  # irrelevant to this assertion, just needs to be valid input
        candles_1d = candles_1h

        replay_bar = target_hour_start + 1  # 2nd 15m bar within the target hour
        warmup = replay_bar  # replay() yields starting at bar_index == warmup_bars
        snapshots = list(smc_replay.replay(candles_15m, candles_1h, candles_4h, candles_1d, warmup_bars=warmup))
        self.assertTrue(snapshots)

        snap = snapshots[0]
        self.assertEqual(snap["bar_index"], replay_bar)

        last_1h = snap["candles_1h"][-1]
        # Only the 2 bars of that hour seen so far (indices target_hour_start,
        # target_hour_start+1) are visible — both have high=100.5, never 500.
        self.assertEqual(last_1h["high"], 100.5)
        self.assertNotEqual(last_1h["high"], 500.0)
        # And its timestamp is the period's open, not some future bar's.
        self.assertEqual(last_1h["timestamp"], _ts(target_hour_start))

    def test_forming_candle_aggregation_is_correct(self):
        n = 20
        candles_15m = [C(_ts(i), 100 + i, 101 + i, 99 + i, 100.5 + i, v=10 + i) for i in range(n)]
        # Bar 9 is the 2nd 15m bar of the 3rd hour (hour boundaries at 0,4,8,12,16).
        replay_bar = 9
        candles_1h = []  # no closed hours at all — forces every window to be all-forming/empty
        snapshots = list(smc_replay.replay(candles_15m, candles_1h, candles_1h, candles_1h, warmup_bars=replay_bar))
        snap = snapshots[0]
        self.assertEqual(snap["bar_index"], replay_bar)

        period_open_idx = 8  # floor(9/4)*4 == 8
        expected_slice = candles_15m[period_open_idx:replay_bar + 1]
        forming = snap["candles_1h"][-1]
        self.assertEqual(forming["timestamp"], _ts(period_open_idx))
        self.assertEqual(forming["open"], expected_slice[0]["open"])
        self.assertEqual(forming["high"], max(c["high"] for c in expected_slice))
        self.assertEqual(forming["low"], min(c["low"] for c in expected_slice))
        self.assertEqual(forming["close"], expected_slice[-1]["close"])
        self.assertEqual(forming["volume"], sum(c["volume"] for c in expected_slice))

    def test_window_sizes_still_capped(self):
        n = 600
        candles_15m = [C(_ts(i), i, i + 1, i - 1, i) for i in range(n)]
        candles_1h = candles_15m
        snapshots = list(smc_replay.replay(candles_15m, candles_1h, candles_1h, candles_1h, warmup_bars=150))
        last = snapshots[-1]
        self.assertLessEqual(len(last["candles_15m"]), smc_replay.WINDOW_15M)
        self.assertLessEqual(len(last["candles_1h"]), smc_replay.WINDOW_1H)
        self.assertLessEqual(len(last["candles_4h"]), smc_replay.WINDOW_4H)
        self.assertLessEqual(len(last["candles_1d"]), smc_replay.WINDOW_1D)

    def test_bisect_windowing_matches_naive_reference_on_random_data(self):
        """Independent, deliberately-naive (full linear scan, no bisect)
        reimplementation of the no-lookahead rule, cross-checked against
        smc_replay.replay on randomized data."""
        rng = random.Random(42)
        n = 300
        candles_15m = [C(_ts(i), 100 + rng.random(), 101 + rng.random(), 99 + rng.random(), 100 + rng.random())
                       for i in range(n)]
        # Build "real" 1h/4h/1d series by full aggregation of the 15m data
        # (as a genuine historical fetch would return), same helper as above.
        def build_htf(duration_bars):
            out = []
            for h in range(n // duration_bars):
                chunk = candles_15m[h * duration_bars:(h + 1) * duration_bars]
                if len(chunk) < duration_bars:
                    break
                out.append(C(chunk[0]["timestamp"], chunk[0]["open"],
                              max(c["high"] for c in chunk), min(c["low"] for c in chunk),
                              chunk[-1]["close"], sum(c["volume"] for c in chunk)))
            return out

        candles_1h = build_htf(4)
        candles_4h = build_htf(16)
        candles_1d = build_htf(96)

        def naive_window(htf_candles, duration_ms, as_of_close_ms, period_open_ms, period_closed, size):
            closed = [c for c in htf_candles
                      if _epoch_ms(c["timestamp"]) + duration_ms <= as_of_close_ms]
            if period_closed:
                combined = closed
            else:
                forming_slice = [c for c in candles_15m
                                  if period_open_ms <= _epoch_ms(c["timestamp"]) <= t_ms]
                forming = {
                    "timestamp": _iso_ms(period_open_ms),
                    "open": forming_slice[0]["open"],
                    "high": max(c["high"] for c in forming_slice),
                    "low": min(c["low"] for c in forming_slice),
                    "close": forming_slice[-1]["close"],
                    "volume": sum(c["volume"] for c in forming_slice),
                }
                combined = closed + [forming]
            return combined[-size:]

        warmup = 150
        snapshots = list(smc_replay.replay(candles_15m, candles_1h, candles_4h, candles_1d, warmup_bars=warmup))
        sampled_indices = rng.sample(range(len(snapshots)), min(20, len(snapshots)))

        for idx in sampled_indices:
            snap = snapshots[idx]
            i = snap["bar_index"]
            t_ms = _epoch_ms(candles_15m[i]["timestamp"])
            as_of_close_ms = t_ms + 15 * 60 * 1000

            for name, htf_candles, duration_ms, size in (
                ("candles_1h", candles_1h, 60 * 60 * 1000, smc_replay.WINDOW_1H),
                ("candles_4h", candles_4h, 4 * 60 * 60 * 1000, smc_replay.WINDOW_4H),
                ("candles_1d", candles_1d, 24 * 60 * 60 * 1000, smc_replay.WINDOW_1D),
            ):
                period_open_ms = (t_ms // duration_ms) * duration_ms
                period_closed = (period_open_ms + duration_ms) <= as_of_close_ms
                expected = naive_window(htf_candles, duration_ms, as_of_close_ms, period_open_ms, period_closed, size)
                self.assertEqual(snap[name], expected, f"mismatch on {name} at bar {i}")


def _epoch_ms(ts: str) -> int:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# trade_simulator — event-driven order/position lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def _flat(n, price=102.0, step_minutes=15):
    return [C(_ts(i, step_minutes), price, price + 1, price - 1, price) for i in range(n)]


def _long_signal(bar_index, entry=100.0, sl=95.0, tp1=105.0, tp2=110.0):
    return {"bar_index": bar_index, "timestamp": _ts(bar_index), "direction": "LONG",
            "entry_price": entry, "stop_loss": sl, "tp1": tp1, "tp2": tp2, "confidence": 90}


class TestSimConfigValidation(unittest.TestCase):
    def test_bad_sizing_mode_raises(self):
        with self.assertRaises(ValueError):
            SimConfig(sizing_mode="not_a_mode")

    def test_non_positive_leverage_raises(self):
        with self.assertRaises(ValueError):
            SimConfig(leverage=0)

    def test_non_positive_risk_pct_for_risk_pct_mode_raises(self):
        with self.assertRaises(ValueError):
            SimConfig(sizing_mode="risk_pct", risk_pct=0)


class TestOrderLifecycle(unittest.TestCase):
    def test_limit_long_fills_on_low_touch_fill_price_is_min_open_entry(self):
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)  # low touches entry(100), open(102) > entry
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["filled"], 1)
        self.assertEqual(r["trades"][0]["entry_price"], 100.0)  # min(open=102, entry=100)

    def test_limit_long_fills_at_open_on_gap_down(self):
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 90, 91, 89, 90.5)  # gapped straight through entry(100)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["trades"][0]["entry_price"], 90.0)  # min(open=90, entry=100)

    def test_ttl_cancels_unfilled_order(self):
        n = 20
        candles = _flat(n)  # price stays at 102, never touches entry(100) or tp1(105)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=5))
        self.assertEqual(r["orders"]["cancelled"]["ttl"], 1)
        self.assertEqual(r["total_trades"], 0)

    def test_tp1_before_fill_cancels(self):
        n = 20
        candles = _flat(n)
        candles[5] = C(_ts(5), 102, 106, 101, 102)  # touches tp1(105) but never entry(100)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["cancelled"]["tp1_before_fill"], 1)
        self.assertEqual(r["total_trades"], 0)

    def test_bar_touching_both_tp1_and_entry_is_treated_as_fill(self):
        n = 20
        candles = _flat(n)
        # Same bar: low dips to entry(100), high reaches tp1(105).
        candles[5] = C(_ts(5), 102, 106, 99.5, 102)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["filled"], 1)
        self.assertEqual(r["orders"]["cancelled"]["tp1_before_fill"], 0)

    def test_bias_flip_cancels_pending_order(self):
        n = 20
        candles = _flat(n)
        signals = [_long_signal(2)]
        bias = {i: ("bearish" if i >= 4 else "bullish") for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["cancelled"]["bias_flip"], 1)
        self.assertEqual(r["total_trades"], 0)

    def test_replacement_cancels_old_and_places_new(self):
        # Different entry/SL/TP -> a genuinely new setup, still counted as "replaced".
        n = 20
        candles = _flat(n)
        signals = [_long_signal(2), _long_signal(4, entry=150, sl=145, tp1=160, tp2=170)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["cancelled"]["replaced"], 1)
        self.assertEqual(r["orders"]["placed"], 2)
        self.assertEqual(r["orders"]["duplicate_signals"], 0)

    def test_duplicate_signals_do_not_reset_ttl(self):
        # Identical signal re-firing every bar the setup persists (bars 2-6)
        # must not reset the TTL clock — the order should still expire on
        # schedule from its FIRST placement (active_from = 3, ttl=5 -> cancel
        # at bar 7), not get "replaced" 4 times and live indefinitely.
        n = 20
        candles = _flat(n)  # never touches entry(100) anywhere
        signals = [_long_signal(b) for b in range(2, 7)]  # bars 2,3,4,5,6 — identical params
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=5))
        self.assertEqual(r["orders"]["placed"], 1)
        self.assertEqual(r["orders"]["cancelled"]["replaced"], 0)
        self.assertEqual(r["orders"]["duplicate_signals"], 4)  # bars 3,4,5,6
        self.assertEqual(r["orders"]["cancelled"]["ttl"], 1)
        self.assertEqual(r["total_trades"], 0)

    def test_ttl_gives_exactly_n_fill_chances(self):
        # ttl=2: fill chances are bars active_from(=3) and active_from+1(=4)
        # only. Bar 5 dips through entry, but by then the order must already
        # be cancelled — a lingering off-by-one would let it fill there.
        n = 20
        candles = _flat(n)
        candles[5] = C(_ts(5), 102, 103, 99.5, 100.5)  # would fill entry(100) if still pending
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=2))
        self.assertEqual(r["orders"]["filled"], 0)
        self.assertEqual(r["orders"]["cancelled"]["ttl"], 1)
        self.assertEqual(r["total_trades"], 0)

    def test_new_signal_while_position_open_is_ignored(self):
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)  # fills at bar 6
        signals = [_long_signal(2), _long_signal(8)]  # 2nd signal arrives while position open
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["ignored_in_position"], 1)
        self.assertEqual(r["orders"]["placed"], 1)


class TestSetupReentryBlocking(unittest.TestCase):
    """A (direction, entry, SL, TP1, TP2) setup is placed at most once by
    default, however the earlier order was resolved — cancelled (ttl,
    tp1_before_fill, bias_flip, replaced) or filled and closed."""

    def test_ttl_cycle_blocks_all_later_identical_signals(self):
        # 60 identical signals (bars 2-61), price never touches entry. Only
        # the very first is ever placed; it expires via ttl exactly once;
        # every other signal bar is either a duplicate_signal (while that
        # first order is still pending) or setup_reused_blocked (after it's
        # gone) — never a fresh placement.
        n = 70
        candles = _flat(n)
        signals = [_long_signal(b) for b in range(2, 62)]  # bars 2..61, identical params
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=5))
        self.assertEqual(r["orders"]["placed"], 1)
        self.assertEqual(r["orders"]["cancelled"]["ttl"], 1)
        self.assertEqual(r["orders"]["cancelled"]["replaced"], 0)
        self.assertEqual(
            r["orders"]["duplicate_signals"] + r["orders"]["setup_reused_blocked"], 59
        )
        self.assertEqual(r["total_trades"], 0)

    def test_tp1_before_fill_then_identical_signal_not_replaced(self):
        n = 20
        candles = _flat(n)
        candles[4] = C(_ts(4), 102, 106, 101, 102)  # reaches tp1(105) without ever touching entry(100)
        signals = [_long_signal(2), _long_signal(6)]  # identical setup, re-fires after cancellation
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["placed"], 1)
        self.assertEqual(r["orders"]["cancelled"]["tp1_before_fill"], 1)
        self.assertEqual(r["orders"]["setup_reused_blocked"], 1)
        self.assertEqual(r["total_trades"], 0)

    def test_sl_then_identical_signal_no_second_trade(self):
        n = 20
        candles = _flat(n)
        candles[4] = C(_ts(4), 102, 103, 99.5, 100.5)  # fill
        candles[5] = C(_ts(5), 100, 101, 94, 95)        # SL hit, position closes
        signals = [_long_signal(2), _long_signal(8)]    # identical setup, re-fires after the trade closed
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["placed"], 1)
        self.assertEqual(r["total_trades"], 1)
        self.assertEqual(r["trades"][0]["outcome"], "sl")
        self.assertEqual(r["orders"]["setup_reused_blocked"], 1)

    def test_different_entry_after_cancel_is_placed_normally(self):
        n = 20
        candles = _flat(n)  # never touches either entry
        signals = [_long_signal(2), _long_signal(8, entry=150, sl=145, tp1=160, tp2=170)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=3))
        self.assertEqual(r["orders"]["placed"], 2)
        self.assertEqual(r["orders"]["setup_reused_blocked"], 0)

    def test_allow_setup_reentry_restores_old_behavior(self):
        n = 70
        candles = _flat(n)
        signals = [_long_signal(b) for b in range(2, 62)]
        bias = {i: "bullish" for i in range(n)}
        cfg = SimConfig(order_ttl_bars=5, allow_setup_reentry=True)
        r = trade_simulator.simulate(candles, signals, bias, cfg)
        self.assertEqual(r["orders"]["setup_reused_blocked"], 0)
        self.assertGreater(r["orders"]["placed"], 1)  # re-placed after each ttl cycle
        self.assertGreaterEqual(r["orders"]["cancelled"]["ttl"], 1)

    def test_allow_setup_reentry_exposed_in_sim_config_output(self):
        cfg_dict = engine._sim_config_as_dict(SimConfig(allow_setup_reentry=True))
        self.assertTrue(cfg_dict["allow_setup_reentry"])
        cfg_dict_default = engine._sim_config_as_dict(SimConfig())
        self.assertFalse(cfg_dict_default["allow_setup_reentry"])


class TestPositionEvents(unittest.TestCase):
    def test_sl_and_tp1_same_bar_prioritizes_sl(self):
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)  # fills entry(100)
        # Next bar: range covers both SL(95) and TP1(105) — SL wins.
        candles[7] = C(_ts(7), 100, 106, 94, 100)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["trades"][0]["outcome"], "sl")

    def test_fill_bar_only_evaluates_sl_not_tp1(self):
        n = 20
        candles = _flat(n)
        # Fill bar itself also reaches tp1 — must NOT be treated as a TP1 event
        # on this bar (only SL is evaluated on the fill bar).
        candles[6] = C(_ts(6), 102, 106, 99.5, 102)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["orders"]["filled"], 1)
        self.assertFalse(r["trades"][0]["outcome"] in ("tp1_be",))  # tp1 wasn't processed on the fill bar
        # position should still be open after the fill bar in this scenario,
        # closing later — confirm no trade finalized on tp1 alone same-bar.

    def test_tp1_then_breakeven_outcome(self):
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)  # fill
        candles[7] = C(_ts(7), 100.5, 106, 100, 105.5)  # tp1 hit, SL -> BE(100)
        candles[8] = C(_ts(8), 105.5, 106, 99, 100)      # price falls back to BE(100)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["trades"][0]["outcome"], "tp1_be")

    def test_tp1_then_tp2_outcome(self):
        # tp2 strictly beyond tp1 -> deferred behavior: TP1 partial on bar 7,
        # remainder closes on a LATER bar (8) once TP2 is actually reached.
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)
        candles[7] = C(_ts(7), 100.5, 106, 100, 105.5)
        candles[8] = C(_ts(8), 105.5, 111, 105, 110.5)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["trades"][0]["outcome"], "tp2")
        self.assertEqual(r["trades"][0]["exit_time"], _ts(8))  # closed on TP2's bar, not TP1's

    def test_tp1_equals_tp2_closes_full_position_on_tp1_bar(self):
        # Live places TP1 and TP2 as two take_profit_market orders at the same
        # price when there's no liquidity target beyond TP1 — both trigger
        # together, so the whole position must close on that single bar.
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)  # fill
        candles[7] = C(_ts(7), 100.5, 106, 100, 105.5)  # reaches tp1==tp2 (105)
        signals = [_long_signal(2, tp1=105.0, tp2=105.0)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(len(r["trades"]), 1)
        trade = r["trades"][0]
        self.assertEqual(trade["outcome"], "tp2")
        self.assertEqual(trade["exit_time"], _ts(7))  # closed same bar as TP1/TP2 hit
        # Full qty closed at ~105 (minus slippage), not just tp1_fraction (50%).
        _, qty, _ = trade_simulator._size_position(1000.0, 100.0, 95.0, SimConfig())
        self.assertAlmostEqual(trade["qty"], qty, places=6)
        self.assertGreater(trade["pnl"], 0)
        # Sanity: a full-qty exit near 105 nets roughly double a half-qty exit at 105.
        half_qty_pnl_approx = (qty / 2) * (105 * (1 - 0.0005) - 100)
        self.assertGreater(trade["pnl"], half_qty_pnl_approx * 1.5)

    def test_sl_gap_exit_at_open(self):
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)  # fill at 100
        candles[7] = C(_ts(7), 90, 91, 88, 89)  # gapped straight through SL(95)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["trades"][0]["outcome"], "sl")
        # exit_price should reflect a base of open(90) with adverse slippage, not sl(95)
        self.assertLess(r["trades"][0]["exit_price"], 91)

    def test_eod_close_when_never_hit_tp1(self):
        n = 10
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["trades"][0]["outcome"], "eod")

    def test_eod_close_after_tp1_hit(self):
        n = 10
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)
        candles[7] = C(_ts(7), 100.5, 106, 100, 105.5)  # tp1 hit
        for i in range(8, n):
            candles[i] = C(_ts(i), 102, 103, 101, 102)  # drifts sideways, never hits BE-SL or tp2
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["trades"][0]["outcome"], "tp1_eod")


class TestRMultipleAndFees(unittest.TestCase):
    def test_hand_computed_tp1_then_tp2(self):
        n = 12
        candles = _flat(n)
        candles[6] = C(_ts(6), 100, 101, 99.5, 100.5)
        candles[7] = C(_ts(7), 100.5, 106, 100, 105.5)
        candles[8] = C(_ts(8), 105.5, 111, 105, 110.5)
        for i in range(9, n):
            candles[i] = C(_ts(i), 110, 111, 109, 110)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig())
        trade = r["trades"][0]
        # Hand-computed: equity=1000, risk_pct=1% => risk_usdt=10, sl_dist_pct=0.05,
        # leverage=10 => margin=20, qty=2.0. entry_fee=maker_fee_pct(0)*2*100=0.
        # tp1 leg (1.0 @ 105*(1-0.0005)=104.9475): fee=taker_fee_pct(0.0002)*104.9475=0.0209895,
        #   net=4.9475-0.0209895=4.9265105.
        # tp2 leg (1.0 @ 110*(1-0.0005)=109.945): fee=0.0002*109.945=0.021989,
        #   net=9.945-0.021989=9.923011.
        # pnl = -0+4.9265105+9.923011 = 14.8495215; risk_usdt(actual)=2*5=10.
        self.assertAlmostEqual(trade["margin"], 20.0, places=2)
        self.assertAlmostEqual(trade["qty"], 2.0, places=4)
        self.assertAlmostEqual(trade["fees"], 0.0430, places=3)
        self.assertAlmostEqual(trade["pnl"], 14.8495, places=2)
        self.assertAlmostEqual(trade["r_multiple"], 1.4850, places=2)


class TestShortDirection(unittest.TestCase):
    def test_short_round_trip_tp1_then_tp2(self):
        n = 15
        candles = [C(_ts(i), 98, 99, 97, 98) for i in range(n)]
        candles[6] = C(_ts(6), 98, 100.5, 97, 100)   # high touches entry(100) -> SHORT fills
        candles[7] = C(_ts(7), 100, 100, 94.5, 95)   # dips through tp1(95)
        candles[8] = C(_ts(8), 95, 96, 89.5, 90)     # dips through tp2(90)
        signals = [{"bar_index": 2, "timestamp": _ts(2), "direction": "SHORT",
                    "entry_price": 100.0, "stop_loss": 105.0, "tp1": 95.0, "tp2": 90.0, "confidence": 90}]
        bias = {i: "bearish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=20))
        self.assertEqual(r["trades"][0]["outcome"], "tp2")
        self.assertGreater(r["trades"][0]["pnl"], 0)
        self.assertEqual(r["trades"][0]["entry_price"], 100.0)  # max(open=98, entry=100)


class TestSizingUsesOrderPrice(unittest.TestCase):
    def test_gap_fill_sizes_on_order_price_not_fill_price(self):
        # Order entry=100, sl=95 -> sl_dist_pct=0.05 off the ORDER price.
        # Fill gaps down to 90, but margin/qty must match the non-gap case
        # exactly (live sizes at placement time, before the fill is known).
        n = 20
        candles = _flat(n)
        candles[6] = C(_ts(6), 90, 91, 89, 90.5)  # gap straight through entry(100)
        signals = [_long_signal(2)]  # entry=100, sl=95
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig())
        trade = r["trades"][0]
        self.assertEqual(trade["entry_price"], 90.0)  # actual fill price, for PnL
        # margin/qty match the order-price-based calc: equity=1000, risk_pct=1%
        # => risk_usdt=10, sl_dist_pct=(100-95)/100=0.05, leverage=10 => margin=20, qty=2.0
        self.assertAlmostEqual(trade["margin"], 20.0, places=2)
        self.assertAlmostEqual(trade["qty"], 2.0, places=4)


class TestMarginCappedTrades(unittest.TestCase):
    def test_margin_capped_trades_counted(self):
        n = 12
        candles = _flat(n)
        candles[6] = C(_ts(6), 100, 101, 99.5, 100.5)
        candles[7] = C(_ts(7), 100.5, 106, 100, 105.5)
        candles[8] = C(_ts(8), 105.5, 111, 105, 110.5)
        for i in range(9, n):
            candles[i] = C(_ts(i), 110, 111, 109, 110)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        # Tiny equity + huge risk_pct forces the margin cap.
        cfg = SimConfig(init_cash=10.0, sizing_mode="risk_pct", risk_pct=1000.0, leverage=1)
        r = trade_simulator.simulate(candles, signals, bias, cfg)
        self.assertEqual(r["orders"]["margin_capped_trades"], 1)
        self.assertTrue(r["trades"][0]["margin_capped"])

    def test_margin_capped_trades_zero_when_no_cap(self):
        n = 12
        candles = _flat(n)
        candles[6] = C(_ts(6), 100, 101, 99.5, 100.5)
        candles[7] = C(_ts(7), 100.5, 106, 100, 105.5)
        candles[8] = C(_ts(8), 105.5, 111, 105, 110.5)
        for i in range(9, n):
            candles[i] = C(_ts(i), 110, 111, 109, 110)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig())
        self.assertEqual(r["orders"]["margin_capped_trades"], 0)


class TestSizing(unittest.TestCase):
    def test_risk_pct_mode(self):
        margin, qty, capped = trade_simulator._size_position(
            equity=1000.0, fill_price=100.0, stop_loss=95.0,
            config=SimConfig(sizing_mode="risk_pct", risk_pct=2.0, leverage=10),
        )
        # risk_usdt=20, sl_dist_pct=0.05, margin=20/(0.05*10)=40
        self.assertAlmostEqual(margin, 40.0, places=4)
        self.assertAlmostEqual(qty, 4.0, places=4)
        self.assertFalse(capped)

    def test_fixed_risk_usdt_mode(self):
        margin, qty, capped = trade_simulator._size_position(
            equity=1000.0, fill_price=100.0, stop_loss=95.0,
            config=SimConfig(sizing_mode="fixed_risk_usdt", fixed_risk_usdt=5.0, leverage=10),
        )
        # margin=5/(0.05*10)=10
        self.assertAlmostEqual(margin, 10.0, places=4)
        self.assertAlmostEqual(qty, 1.0, places=4)
        self.assertFalse(capped)

    def test_fixed_margin_usdt_mode(self):
        margin, qty, capped = trade_simulator._size_position(
            equity=1000.0, fill_price=100.0, stop_loss=95.0,
            config=SimConfig(sizing_mode="fixed_margin_usdt", fixed_margin_usdt=7.5, leverage=10),
        )
        self.assertAlmostEqual(margin, 7.5, places=4)
        self.assertAlmostEqual(qty, 0.75, places=4)
        self.assertFalse(capped)

    def test_margin_capped_at_equity(self):
        margin, qty, capped = trade_simulator._size_position(
            equity=10.0, fill_price=100.0, stop_loss=99.0,  # very tight SL -> huge margin request
            config=SimConfig(sizing_mode="risk_pct", risk_pct=100.0, leverage=1),
        )
        self.assertTrue(capped)
        self.assertAlmostEqual(margin, 10.0, places=4)


class TestResultJsonSafety(unittest.TestCase):
    def test_zero_trades_is_json_serializable(self):
        import json
        n = 20
        candles = _flat(n)  # never touches entry — no fills
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=5))
        self.assertEqual(r["total_trades"], 0)
        self.assertIsNone(r["profit_factor"])
        json.dumps(r)

    def test_all_winners_profit_factor_none(self):
        import json
        n = 12
        candles = _flat(n)
        candles[6] = C(_ts(6), 100, 101, 99.5, 100.5)
        candles[7] = C(_ts(7), 100.5, 106, 100, 105.5)
        candles[8] = C(_ts(8), 105.5, 111, 105, 110.5)
        for i in range(9, n):
            candles[i] = C(_ts(i), 110, 111, 109, 110)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig())
        self.assertEqual(r["total_trades"], 1)
        self.assertGreater(r["trades"][0]["pnl"], 0)
        self.assertIsNone(r["profit_factor"])  # no losses
        json.dumps(r)


# ─────────────────────────────────────────────────────────────────────────────
# engine — sim_mode dispatch
# ─────────────────────────────────────────────────────────────────────────────

class TestEngineSimModeDispatch(unittest.TestCase):
    """Patches the replay/signal layer so engine.run_backtest's dispatch logic
    (event vs. vbt_legacy, bias_by_bar recording, result-key wiring) can be
    tested without needing a real market-structure setup or network access."""

    def _fake_snapshots(self, candles_15m, trade_bar_indices):
        for i, bar in enumerate(candles_15m):
            yield {
                "bar_index": i, "timestamp": bar["timestamp"],
                "open_price": bar["open"], "high_price": bar["high"],
                "low_price": bar["low"], "close_price": bar["close"],
                "candles_1d": [], "candles_4h": [], "candles_1h": [], "candles_15m": [],
                "smc_levels": {
                    "key_levels": [],
                    "confluence": {"score": 90, "factors": {"structure_1D": "bullish"}, "conflicts": []},
                },
                "kill_zone": "london",
            }

    def _run_with_mocks(self, sim_mode):
        n = 400  # >= MIN_BARS_REQUIRED(200) + DEFAULT_WARMUP_BARS(120) after the fallback-warmup subtraction
        candles = _flat(n)
        candles[100] = C(_ts(100), 102, 103, 99.5, 100.5)  # fills the signal at bar 96

        def fake_replay(*args, **kwargs):
            return self._fake_snapshots(candles, {96})

        def fake_has_structural_setup(smc_levels):
            return True

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {
                "decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None,
            }

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=fake_replay), \
             patch.object(engine.smc_engine, "has_structural_setup", side_effect=fake_has_structural_setup), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal):
            return engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                sim_mode=sim_mode,
            )

    def test_event_mode_result_shape(self):
        result = self._run_with_mocks("event")
        self.assertEqual(result["sim_mode"], "event")
        self.assertIsNotNone(result["sim_config"])
        self.assertIsNotNone(result["orders"])
        self.assertIn("expectancy_r", result)
        self.assertIn("avg_win_r", result)
        self.assertIn("avg_loss_r", result)
        self.assertGreaterEqual(result["total_trades"], 0)

    def test_vbt_legacy_mode_still_produces_old_result_keys(self):
        result = self._run_with_mocks("vbt_legacy")
        self.assertEqual(result["sim_mode"], "vbt_legacy")
        for key in ("final_equity", "total_return_pct", "win_rate_pct", "sharpe_ratio",
                    "max_drawdown_pct", "profit_factor", "total_trades", "trades", "equity_curve"):
            self.assertIn(key, result)
        self.assertIsNone(result["sim_config"])

    def test_invalid_sim_mode_raises(self):
        with self.assertRaises(ValueError):
            engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                sim_mode="not_a_mode",
            )


# ─────────────────────────────────────────────────────────────────────────────
# router / CLI — risk_pct and leverage default from settings when omitted
# ─────────────────────────────────────────────────────────────────────────────

_EMPTY_ENGINE_RESULT_TEMPLATE = {
    "since": "2026-01-01T00:00:00+00:00", "until": "2026-01-02T00:00:00+00:00",
    "bars_analyzed": 0, "signals_generated": 0, "total_trades": 0,
    "final_equity": None, "total_return_pct": 0.0, "win_rate_pct": 0.0,
    "sharpe_ratio": 0.0, "max_drawdown_pct": 0.0, "profit_factor": 0.0,
    "expectancy_r": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0,
    "trades": [], "equity_curve": [], "claude_sample": [], "orders": None,
    "sim_config": None, "note": None,
    "decision_schedule": "every_bar", "decision_bars": 0,
    "signal_config": {"min_sl_pct": None, "min_sl_atr": None, "atr_period": 14},
    "sl_pct_stats": None, "cost_r_stats": None, "fill_bar_sl_exits": 0, "r_by_sl_bucket": {},
}


class TestSettingsDefaultsForRiskAndLeverage(unittest.TestCase):
    def test_router_pulls_risk_pct_and_leverage_from_settings_when_omitted(self):
        import asyncio
        from app.routers import backtest as backtest_router

        captured = {}

        def fake_run_backtest(**kwargs):
            captured.update(kwargs)
            return {"symbol": kwargs["symbol"], "sim_mode": kwargs["sim_mode"], **_EMPTY_ENGINE_RESULT_TEMPLATE}

        req = backtest_router.BacktestRequest(
            symbol="BTC/USDT",
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            until=datetime(2026, 1, 2, tzinfo=timezone.utc),
            save=False,
            # risk_pct/leverage intentionally omitted -> None -> from settings
        )
        with patch.object(backtest_router, "run_backtest", side_effect=fake_run_backtest), \
             patch.object(_settings, "risk_per_trade_pct", 7.0), \
             patch.object(_settings, "max_leverage", 33):
            asyncio.run(backtest_router.run_backtest_endpoint(req))

        self.assertIsNotNone(captured["sim_config"])
        self.assertEqual(captured["sim_config"].risk_pct, 7.0)
        self.assertEqual(captured["sim_config"].leverage, 33)

    def test_router_respects_explicit_risk_pct_and_leverage(self):
        import asyncio
        from app.routers import backtest as backtest_router

        captured = {}

        def fake_run_backtest(**kwargs):
            captured.update(kwargs)
            return {"symbol": kwargs["symbol"], "sim_mode": kwargs["sim_mode"], **_EMPTY_ENGINE_RESULT_TEMPLATE}

        req = backtest_router.BacktestRequest(
            symbol="BTC/USDT",
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            until=datetime(2026, 1, 2, tzinfo=timezone.utc),
            save=False, risk_pct=2.5, leverage=7,
        )
        with patch.object(backtest_router, "run_backtest", side_effect=fake_run_backtest), \
             patch.object(_settings, "risk_per_trade_pct", 7.0), \
             patch.object(_settings, "max_leverage", 33):
            asyncio.run(backtest_router.run_backtest_endpoint(req))

        self.assertEqual(captured["sim_config"].risk_pct, 2.5)
        self.assertEqual(captured["sim_config"].leverage, 7)

    def test_cli_pulls_risk_pct_and_leverage_from_settings_when_omitted(self):
        from app.scripts import run_backtest as cli

        captured = {}

        def fake_run_backtest(**kwargs):
            captured.update(kwargs)
            return {"symbol": kwargs["symbol"], "sim_mode": kwargs["sim_mode"], **_EMPTY_ENGINE_RESULT_TEMPLATE}

        with patch.object(cli, "_ensure_running_from_backend", return_value=None), \
             patch.object(cli, "run_backtest", side_effect=fake_run_backtest), \
             patch.object(_settings, "risk_per_trade_pct", 8.0), \
             patch.object(_settings, "max_leverage", 44):
            cli.main(["--symbol", "BTC/USDT", "--since", "2026-01-01", "--until", "2026-01-02", "--no-save"])

        self.assertEqual(captured["sim_config"].risk_pct, 8.0)
        self.assertEqual(captured["sim_config"].leverage, 44)

    def test_cli_respects_explicit_risk_pct_and_leverage(self):
        from app.scripts import run_backtest as cli

        captured = {}

        def fake_run_backtest(**kwargs):
            captured.update(kwargs)
            return {"symbol": kwargs["symbol"], "sim_mode": kwargs["sim_mode"], **_EMPTY_ENGINE_RESULT_TEMPLATE}

        with patch.object(cli, "_ensure_running_from_backend", return_value=None), \
             patch.object(cli, "run_backtest", side_effect=fake_run_backtest), \
             patch.object(_settings, "risk_per_trade_pct", 8.0), \
             patch.object(_settings, "max_leverage", 44):
            cli.main(["--symbol", "BTC/USDT", "--since", "2026-01-01", "--until", "2026-01-02",
                      "--no-save", "--risk-pct", "3.0", "--leverage", "6"])

        self.assertEqual(captured["sim_config"].risk_pct, 3.0)
        self.assertEqual(captured["sim_config"].leverage, 6)


class TestCliRunsFromBackendDir(unittest.TestCase):
    def test_ensure_running_from_backend_exits_when_cwd_mismatched(self):
        from app.scripts import run_backtest as cli

        with patch.object(cli.Path, "cwd", return_value=Path("/definitely/not/backend")):
            with self.assertRaises(SystemExit):
                cli._ensure_running_from_backend()

    def test_ensure_running_from_backend_passes_when_cwd_matches(self):
        from app.scripts import run_backtest as cli

        with patch.object(cli.Path, "cwd", return_value=cli.BACKEND_DIR):
            cli._ensure_running_from_backend()  # must not raise


if __name__ == "__main__":
    unittest.main()
