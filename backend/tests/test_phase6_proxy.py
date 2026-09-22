"""
Unit tests Phase 6 — backtest proxy realism (no real exchange/network calls,
no real Claude API calls). Mirrors the stubbing pattern used in
test_phase4.py / test_phase6_fidelity.py so these run without a .env file.

Covers: maker/taker fees, SignalConfig's minimum risk-distance floor (pct and
ATR), decision_schedule="daily" cadence, Claude calibration sampling
(claude_sample_max, error handling, placeholder-key guard), the new
diagnostics keys (sl_pct_stats/cost_r_stats/fill_bar_sl_exits/r_by_sl_bucket),
and the _matches_any_setup set-based rewrite.
"""
import json
import os
import sys
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

_settings = sys.modules["app.config"].settings
_settings.anthropic_api_key = "sk-test-stub"  # intentionally short/placeholder-like by default
_settings.claude_model = "claude-sonnet-4-6"
_settings.binance_api_key = ""
_settings.binance_api_secret = ""
_settings.database_url = f"sqlite:///{_TMP_DB.name}"
_settings.risk_per_trade_pct = 1.0
_settings.max_leverage = 10
_settings.timezone = "Asia/Jakarta"
_settings.daily_analysis_time = "08:00"

from app.models.database import init_db  # noqa: E402
from app.services.backtest import engine, signal_simulator, trade_simulator  # noqa: E402
from app.services.backtest.signal_simulator import SignalConfig  # noqa: E402
from app.services.backtest.trade_simulator import SimConfig  # noqa: E402

init_db()

_REAL_ANTHROPIC_KEY = "sk-test-stub-not-a-real-key-but-20chars"  # len >= 20, not "dummy"


def C(ts, o, h, l, c, v=100.0):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _ts(i, step_minutes=15, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
    return (start + timedelta(minutes=i * step_minutes)).isoformat()


def _flat(n, price=102.0, step_minutes=15):
    return [C(_ts(i, step_minutes), price, price + 1, price - 1, price) for i in range(n)]


def _long_signal(bar_index, entry=100.0, sl=95.0, tp1=105.0, tp2=110.0):
    return {"bar_index": bar_index, "timestamp": _ts(bar_index), "direction": "LONG",
            "entry_price": entry, "stop_loss": sl, "tp1": tp1, "tp2": tp2, "confidence": 90}


def _bullish_levels(score=90):
    """Same fixture shape as test_phase4.py's TestSignalSimulator — risk ~2.3
    (2.3958% of entry) on the default (no-floor) path."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Maker/taker fees — trade_simulator.py
# ─────────────────────────────────────────────────────────────────────────────

class TestMakerTakerFees(unittest.TestCase):
    def test_maker_fee_on_entry_taker_fee_on_exits_hand_computed(self):
        n = 15
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)  # fills at 100 (min(open, entry))
        candles[7] = C(_ts(7), 100, 101, 94, 100)       # SL touch at 95 (open > sl, raw = sl)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        cfg = SimConfig(maker_fee_pct=0.001, taker_fee_pct=0.002, order_ttl_bars=20)
        r = trade_simulator.simulate(candles, signals, bias, cfg)
        trade = r["trades"][0]
        # qty=2.0 (equity=1000, risk_pct=1%, sl_dist_pct=0.05, leverage=10 -> margin=20).
        # entry_fee = maker_fee_pct(0.001) * 2.0 * 100 = 0.2 (limit fill -> maker).
        # SL exit: raw=95 (no gap), exit_price=95*(1-0.0005)=94.9525 (market -> taker + slippage).
        #   exit_fee = taker_fee_pct(0.002) * 2.0 * 94.9525 = 0.37981.
        # fees_total = 0.2 + 0.37981 = 0.57981.
        self.assertEqual(trade["outcome"], "sl")
        self.assertAlmostEqual(trade["fees"], 0.5798, places=3)

    def test_maker_fee_pct_zero_gives_zero_entry_fee(self):
        n = 15
        candles = _flat(n)
        candles[6] = C(_ts(6), 102, 103, 99.5, 100.5)
        candles[7] = C(_ts(7), 100, 101, 94, 100)
        signals = [_long_signal(2)]
        bias = {i: "bullish" for i in range(n)}
        # maker=0 and taker=0 and slippage=0 isolates: if fees are exactly zero and pnl is the
        # exact raw price delta, the (zero) maker fee contributed nothing on entry.
        cfg = SimConfig(maker_fee_pct=0.0, taker_fee_pct=0.0, slippage_pct=0.0, order_ttl_bars=20)
        r = trade_simulator.simulate(candles, signals, bias, cfg)
        trade = r["trades"][0]
        self.assertEqual(trade["fees"], 0.0)
        self.assertAlmostEqual(trade["pnl"], -10.0, places=6)  # qty(2.0) * (95 - 100), no cost

    def test_sim_config_rejects_negative_fees(self):
        with self.assertRaises(ValueError):
            SimConfig(maker_fee_pct=-0.001)
        with self.assertRaises(ValueError):
            SimConfig(taker_fee_pct=-0.001)


# ─────────────────────────────────────────────────────────────────────────────
# SignalConfig — minimum risk-distance floor (pct)
# ─────────────────────────────────────────────────────────────────────────────

class TestMinSlPct(unittest.TestCase):
    def test_tight_zone_rejected_with_reason(self):
        tight_levels = {
            "key_levels": [
                {"type": "Order Block Bullish", "price": 99.975, "low": 99.95, "high": 100.0, "tf": "1H", "strength": 6},
            ],
            "confluence": {"score": 90, "factors": {"structure_1D": "bullish"}, "conflicts": []},
        }
        cfg = SignalConfig(min_sl_pct=0.3)
        sig = signal_simulator.rule_based_signal(tight_levels, current_price=100.0, config=cfg)
        self.assertEqual(sig["decision"], "NO_TRADE")
        self.assertTrue(sig["no_trade_reason"].startswith("Risk distance below minimum"))

    def test_wide_zone_still_trades(self):
        cfg = SignalConfig(min_sl_pct=0.3)
        sig = signal_simulator.rule_based_signal(_bullish_levels(90), current_price=100, config=cfg)
        self.assertEqual(sig["decision"], "TRADE")

    def test_default_config_matches_pre_existing_phase4_fixtures(self):
        # No config/candles_15m args — signature must stay backward compatible,
        # and behavior must be byte-for-byte the same as before this feature.
        sig = signal_simulator.rule_based_signal(_bullish_levels(90), current_price=100)
        self.assertEqual(sig["decision"], "TRADE")
        self.assertEqual(sig["direction"], "LONG")
        self.assertGreaterEqual(sig["rr_ratio"], signal_simulator.MIN_RR - 0.01)
        self.assertGreaterEqual(sig["confidence"], signal_simulator.MIN_CONFIDENCE)
        self.assertLess(sig["stop_loss"], sig["entry_price"])
        self.assertGreater(sig["tp1"], sig["entry_price"])

        sig_explicit_default = signal_simulator.rule_based_signal(
            _bullish_levels(90), current_price=100, config=SignalConfig(), candles_15m=None,
        )
        self.assertEqual(sig, sig_explicit_default)


# ─────────────────────────────────────────────────────────────────────────────
# SignalConfig — minimum risk-distance floor (ATR)
# ─────────────────────────────────────────────────────────────────────────────

class TestMinSlAtr(unittest.TestCase):
    def _flat_atr_candles(self, n=20):
        # high-low=2, flat close -> true range = 2 for every bar -> ATR(14) = 2.0 exactly.
        return [C(_ts(i), 100, 101, 99, 100) for i in range(n)]

    def test_atr_computed_correctly(self):
        atr = signal_simulator._compute_atr(self._flat_atr_candles(), period=14)
        self.assertAlmostEqual(atr, 2.0, places=6)

    def test_threshold_respected(self):
        candles = self._flat_atr_candles()
        # risk ~= 2.3 (see _bullish_levels docstring)
        cfg_high = SignalConfig(min_sl_atr=2.0)  # needs risk >= 2.0*2.0=4.0 -> rejected
        sig_high = signal_simulator.rule_based_signal(
            _bullish_levels(90), current_price=100, config=cfg_high, candles_15m=candles,
        )
        self.assertEqual(sig_high["decision"], "NO_TRADE")
        self.assertTrue(sig_high["no_trade_reason"].startswith("Risk distance below minimum"))

        cfg_low = SignalConfig(min_sl_atr=0.5)  # needs risk >= 0.5*2.0=1.0 -> passes
        sig_low = signal_simulator.rule_based_signal(
            _bullish_levels(90), current_price=100, config=cfg_low, candles_15m=candles,
        )
        self.assertEqual(sig_low["decision"], "TRADE")

    def test_missing_candles_raises(self):
        cfg = SignalConfig(min_sl_atr=1.0)
        with self.assertRaises(ValueError):
            signal_simulator.rule_based_signal(_bullish_levels(90), current_price=100, config=cfg, candles_15m=None)
        with self.assertRaises(ValueError):
            signal_simulator.rule_based_signal(_bullish_levels(90), current_price=100, config=cfg, candles_15m=[])

    def test_too_short_candles_raises(self):
        cfg = SignalConfig(min_sl_atr=1.0, atr_period=14)
        with self.assertRaises(ValueError):
            signal_simulator.rule_based_signal(
                _bullish_levels(90), current_price=100, config=cfg, candles_15m=self._flat_atr_candles(n=5),
            )


# ─────────────────────────────────────────────────────────────────────────────
# decision_schedule — engine.py
# ─────────────────────────────────────────────────────────────────────────────

class TestDailyDecisionSchedule(unittest.TestCase):
    def test_decision_bar_indices_one_per_day_at_configured_close_time(self):
        n = 500
        candles = [C(_ts(i), 100, 101, 99, 100) for i in range(n)]
        indices = engine._decision_bar_indices(
            candles, warmup_bars=120, decision_schedule="daily",
            tz_name="Asia/Jakarta", daily_time="08:00",
        )
        self.assertEqual(indices, [195, 291, 387, 483])  # verified: 96 bars (1 day) apart
        for idx in indices:
            bar_open = datetime.fromisoformat(candles[idx]["timestamp"])
            bar_close = bar_open + timedelta(minutes=15)
            local_close = bar_close.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Jakarta"))
            self.assertEqual(local_close.hour, 8)
            self.assertEqual(local_close.minute, 0)
        diffs = [b - a for a, b in zip(indices, indices[1:])]
        self.assertTrue(all(d == 96 for d in diffs))  # exactly one per calendar day

    def test_every_bar_schedule_unaffected(self):
        n = 250
        candles = [C(_ts(i), 100, 101, 99, 100) for i in range(n)]
        indices = engine._decision_bar_indices(
            candles, warmup_bars=120, decision_schedule="every_bar",
            tz_name="Asia/Jakarta", daily_time="08:00",
        )
        self.assertEqual(indices, list(range(120, n)))

    def _fake_replay(self, candles):
        def gen(*a, **k):
            for i, bar in enumerate(candles):
                yield {
                    "bar_index": i, "timestamp": bar["timestamp"],
                    "open_price": bar["open"], "high_price": bar["high"],
                    "low_price": bar["low"], "close_price": bar["close"],
                    "candles_1d": [], "candles_4h": [], "candles_1h": [], "candles_15m": [bar],
                    "smc_levels": {"key_levels": [], "confluence": {"score": 0, "factors": {}, "conflicts": []}},
                    "kill_zone": "asia",
                }
        return gen

    def test_daily_schedule_bypasses_prefilter_and_bias_covers_every_bar(self):
        n = 400
        candles = [C(_ts(i), 100, 101, 99, 100) for i in range(n)]

        prefilter_call_count = [0]

        def fake_has_structural_setup(smc_levels):
            prefilter_call_count[0] += 1
            return True

        bias_call_count = [0]

        def fake_primary_bias(confluence):
            bias_call_count[0] += 1
            return "neutral"

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {"decision": "NO_TRADE", "direction": None, "entry_price": None,
                    "stop_loss": None, "tp1": None, "tp2": None,
                    "confidence": 0, "rr_ratio": None, "no_trade_reason": "x"}

        with patch.object(_settings, "timezone", "Asia/Jakarta"), \
             patch.object(_settings, "daily_analysis_time", "08:00"), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=self._fake_replay(candles)), \
             patch.object(engine.smc_engine, "has_structural_setup", side_effect=fake_has_structural_setup), \
             patch.object(engine.signal_simulator, "primary_bias", side_effect=fake_primary_bias), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal):
            result = engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 5, tzinfo=timezone.utc),
                decision_schedule="daily",
            )

        self.assertEqual(prefilter_call_count[0], 0)     # bypassed on every daily decision bar
        self.assertEqual(bias_call_count[0], n)           # bias computed for every replayed bar
        self.assertGreater(result["decision_bars"], 0)
        self.assertEqual(result["decision_schedule"], "daily")

    def test_invalid_decision_schedule_raises(self):
        with self.assertRaises(ValueError):
            engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                decision_schedule="hourly",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Claude calibration sampling — engine.py
# ─────────────────────────────────────────────────────────────────────────────

class TestClaudeSampling(unittest.TestCase):
    def _fake_replay(self, candles):
        def gen(*a, **k):
            for i, bar in enumerate(candles):
                yield {
                    "bar_index": i, "timestamp": bar["timestamp"],
                    "open_price": bar["open"], "high_price": bar["high"],
                    "low_price": bar["low"], "close_price": bar["close"],
                    "candles_1d": [], "candles_4h": [], "candles_1h": [], "candles_15m": [bar],
                    "smc_levels": {"key_levels": [], "confluence": {"score": 90, "factors": {"structure_1D": "bullish"}, "conflicts": []}},
                    "kill_zone": "london",
                }
        return gen

    def _fake_claude_execution(self, decision="TRADE"):
        if decision == "TRADE":
            return {"execution": {
                "decision": "TRADE", "direction": "LONG", "entry_price": 101.0,
                "stop_loss": 96.0, "tp1": 106.0, "tp2": 111.0, "confidence": 88, "rr_ratio": 3.1,
            }}
        return {"execution": {
            "decision": "NO_TRADE", "direction": None, "entry_price": None,
            "stop_loss": None, "tp1": None, "tp2": None, "confidence": 20, "rr_ratio": None,
        }}

    def test_claude_sample_max_evenly_spaced_includes_no_trade_bars(self):
        n = 250
        candles = _flat(n)

        # decision_bars for every_bar over n=250, warmup=120 -> 130 (verified).
        # evenly_spaced_positions(130, 5) == {0, 26, 52, 78, 104} (verified).
        call_idx = [-1]

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            call_idx[0] += 1
            if call_idx[0] == 26:
                return {"decision": "NO_TRADE", "direction": None, "entry_price": None,
                        "stop_loss": None, "tp1": None, "tp2": None,
                        "confidence": 10, "rr_ratio": None, "no_trade_reason": "x"}
            return {"decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                    "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                    "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None}

        claude_calls = []

        def fake_run_analysis(snapshot):
            claude_calls.append(snapshot)
            return self._fake_claude_execution("TRADE")

        with patch.object(_settings, "anthropic_api_key", _REAL_ANTHROPIC_KEY), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=self._fake_replay(candles)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal), \
             patch("app.services.claude_service.run_analysis", side_effect=fake_run_analysis):
            result = engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                claude_sample_max=5,
            )

        self.assertEqual(result["decision_bars"], 130)
        self.assertEqual(len(claude_calls), 5)
        self.assertEqual(len(result["claude_sample"]), 5)

        no_trade_samples = [s for s in result["claude_sample"] if s.get("rule_decision") == "NO_TRADE"]
        self.assertEqual(len(no_trade_samples), 1)  # position 26 was forced to rule NO_TRADE

        # each record carries both sides' prices and sl_pct
        for s in result["claude_sample"]:
            for key in ("timestamp", "rule_decision", "rule_entry", "rule_sl", "rule_sl_pct",
                        "claude_decision", "claude_entry", "claude_sl", "claude_sl_pct",
                        "agree_decision", "agree_direction"):
                self.assertIn(key, s)

    def test_claude_call_exception_recorded_as_error_and_run_continues(self):
        n = 250
        candles = _flat(n)
        candles[125] = C(_ts(125), 102, 103, 99.5, 100.5)  # gives the proxy something to fill
        # (before the identical repeated setup's TTL cancellation would permanently block it)

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {"decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                    "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                    "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None}

        def fake_run_analysis(snapshot):
            raise RuntimeError("simulated Claude API failure")

        with patch.object(_settings, "anthropic_api_key", _REAL_ANTHROPIC_KEY), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=self._fake_replay(candles)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal), \
             patch("app.services.claude_service.run_analysis", side_effect=fake_run_analysis):
            result = engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                claude_sample_max=3,
            )

        self.assertEqual(len(result["claude_sample"]), 3)
        for s in result["claude_sample"]:
            self.assertIn("error", s)
        # the run itself completed and still produced trades from the (unaffected) proxy
        self.assertGreater(result["total_trades"], 0)

    def test_placeholder_api_key_raises_before_replay(self):
        mock_fetch = MagicMock(return_value=_flat(250))
        with patch.object(_settings, "anthropic_api_key", "dummy"), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", mock_fetch):
            with self.assertRaises(ValueError):
                engine.run_backtest(
                    "BTC/USDT",
                    since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    claude_sample_max=1,
                )
        mock_fetch.assert_not_called()

    def test_short_placeholder_api_key_raises(self):
        with patch.object(_settings, "anthropic_api_key", "sk-short"):
            with self.assertRaises(ValueError):
                engine.run_backtest(
                    "BTC/USDT",
                    since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    claude_sample_pct=0.1,
                )

    def test_both_sample_pct_and_max_raises(self):
        with self.assertRaises(ValueError):
            engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                claude_sample_pct=0.5, claude_sample_max=5,
            )

    def test_sampling_does_not_change_proxy_metrics(self):
        n = 250
        candles = _flat(n)
        candles[130] = C(_ts(130), 102, 103, 99.5, 100.5)  # gives the proxy something to fill

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {"decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                    "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                    "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None}

        def run(claude_sample_max):
            kwargs = {}
            patches = [
                patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles),
                patch.object(engine.smc_replay, "replay", side_effect=self._fake_replay(candles)),
                patch.object(engine.smc_engine, "has_structural_setup", return_value=True),
                patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal),
            ]
            if claude_sample_max:
                kwargs["claude_sample_max"] = claude_sample_max
                patches.append(patch.object(_settings, "anthropic_api_key", _REAL_ANTHROPIC_KEY))
                patches.append(patch("app.services.claude_service.run_analysis",
                                      side_effect=lambda s: self._fake_claude_execution("TRADE")))
            for p in patches:
                p.start()
            try:
                return engine.run_backtest(
                    "BTC/USDT",
                    since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    **kwargs,
                )
            finally:
                for p in patches:
                    p.stop()

        result_no_sample = run(claude_sample_max=0)
        result_sampled = run(claude_sample_max=5)

        self.assertEqual(result_no_sample["total_trades"], result_sampled["total_trades"])
        self.assertEqual(result_no_sample["final_equity"], result_sampled["final_equity"])
        self.assertEqual(
            [{k: v for k, v in t.items()} for t in result_no_sample["trades"]],
            [{k: v for k, v in t.items()} for t in result_sampled["trades"]],
        )

    def _fake_fallback_execution(self, reason="Analysis unavailable after 2 attempt(s): Anthropic API error: 401"):
        # claude_service.run_analysis() never raises -- on API/parse failure
        # it returns a structurally valid NO_TRADE fallback instead
        # (claude_service._no_trade_fallback / run_analysis's final
        # except-all block). This is what that looks like.
        return {"execution": {
            "decision": "NO_TRADE", "direction": None, "entry_price": None,
            "stop_loss": None, "tp1": None, "tp2": None, "confidence": 0,
            "rr_ratio": None, "no_trade_reason": reason,
        }}

    def test_claude_fallback_return_recorded_as_error_and_excluded_from_stats(self):
        n = 250
        candles = _flat(n)
        candles[125] = C(_ts(125), 102, 103, 99.5, 100.5)  # gives the proxy something to fill

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {"decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                    "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                    "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None}

        call_idx = [0]

        def fake_run_analysis(snapshot):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return self._fake_fallback_execution()
            return self._fake_claude_execution("TRADE")

        with patch.object(_settings, "anthropic_api_key", _REAL_ANTHROPIC_KEY), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=self._fake_replay(candles)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal), \
             patch("app.services.claude_service.run_analysis", side_effect=fake_run_analysis):
            result = engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                claude_sample_max=2,
            )

        self.assertEqual(len(result["claude_sample"]), 2)
        fallback_sample = result["claude_sample"][0]
        self.assertIn("error", fallback_sample)
        self.assertTrue(fallback_sample["error"].startswith("Analysis unavailable after"))
        for key in ("claude_decision", "claude_direction", "claude_entry", "claude_sl",
                    "agree_decision", "agree_direction"):
            self.assertNotIn(key, fallback_sample)

        # excluded from claude_vs_proxy -- only the 2nd (successful) sample counts
        cvp = result["claude_vs_proxy"]
        self.assertIsNotNone(cvp)
        self.assertEqual(cvp["claude_trade_n"], 1)

    def test_three_consecutive_fallback_failures_raises(self):
        n = 250
        candles = _flat(n)

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {"decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                    "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                    "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None}

        def fake_run_analysis(snapshot):
            return self._fake_fallback_execution("Analysis unavailable after 2 attempt(s): "
                                                   "Anthropic API error: 401 Unauthorized")

        with patch.object(_settings, "anthropic_api_key", _REAL_ANTHROPIC_KEY), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=self._fake_replay(candles)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal), \
             patch("app.services.claude_service.run_analysis", side_effect=fake_run_analysis):
            with self.assertRaises(ValueError) as ctx:
                engine.run_backtest(
                    "BTC/USDT",
                    since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    claude_sample_max=5,
                )
        self.assertIn("bad ANTHROPIC_API_KEY", str(ctx.exception))

    def test_agree_direction_none_unless_both_trade(self):
        n = 250
        candles = _flat(n)
        candles[125] = C(_ts(125), 102, 103, 99.5, 100.5)

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {"decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                    "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                    "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None}

        def fake_run_analysis(snapshot):
            return self._fake_claude_execution("NO_TRADE")  # rule=TRADE, claude=NO_TRADE

        with patch.object(_settings, "anthropic_api_key", _REAL_ANTHROPIC_KEY), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=self._fake_replay(candles)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal), \
             patch("app.services.claude_service.run_analysis", side_effect=fake_run_analysis):
            result = engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                claude_sample_max=1,
            )

        sample = result["claude_sample"][0]
        self.assertEqual(sample["rule_decision"], "TRADE")
        self.assertEqual(sample["claude_decision"], "NO_TRADE")
        self.assertIsNone(sample["agree_direction"])
        self.assertFalse(sample["agree_decision"])

    def test_claude_vs_proxy_sl_pct_stats_populated_for_both_trade_samples(self):
        n = 250
        candles = _flat(n)
        candles[125] = C(_ts(125), 102, 103, 99.5, 100.5)

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {"decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                    "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                    "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None}

        def fake_run_analysis(snapshot):
            return self._fake_claude_execution("TRADE")

        with patch.object(_settings, "anthropic_api_key", _REAL_ANTHROPIC_KEY), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=self._fake_replay(candles)), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal), \
             patch("app.services.claude_service.run_analysis", side_effect=fake_run_analysis):
            result = engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                claude_sample_max=3,
            )

        cvp = result["claude_vs_proxy"]
        self.assertEqual(cvp["both_trade_n"], 3)
        self.assertEqual(cvp["claude_trade_n"], 3)
        self.assertEqual(cvp["decision_agreement_pct"], 100.0)
        self.assertEqual(cvp["direction_agreement_pct"], 100.0)
        self.assertIsNotNone(cvp["rule_sl_pct_stats"])
        self.assertIsNotNone(cvp["claude_sl_pct_stats"])
        for key in ("min", "p25", "median", "p75", "max"):
            self.assertIn(key, cvp["rule_sl_pct_stats"])
            self.assertIn(key, cvp["claude_sl_pct_stats"])


class TestHistoricalTicker(unittest.TestCase):
    def test_derives_high_low_volume_change_pct_from_last_96_bars(self):
        n = 100
        candles = [C(_ts(i), 100, 101, 99, 100, v=10) for i in range(n)]
        candles[50] = C(_ts(50), 100, 150, 99, 100, v=10)   # spike WITHIN the last-96 window (idx >= 4)
        candles[2] = C(_ts(2), 100, 999, 0.5, 100, v=9999)  # OUTSIDE the last-96 window (idx < 4)

        snap = {"close_price": 105.0, "candles_15m": candles}
        ticker = engine._derive_historical_ticker("BTC/USDT", snap)
        window = candles[-96:]

        self.assertEqual(ticker["symbol"], "BTC/USDT")
        self.assertEqual(ticker["last"], 105.0)
        self.assertEqual(ticker["high"], max(c["high"] for c in window))
        self.assertEqual(ticker["high"], 150)
        self.assertNotEqual(ticker["high"], 999)  # bar 2 correctly excluded
        self.assertEqual(ticker["low"], min(c["low"] for c in window))
        self.assertEqual(ticker["volume"], sum(c["volume"] for c in window))
        expected_change_pct = (105.0 - window[0]["open"]) / window[0]["open"] * 100.0
        self.assertAlmostEqual(ticker["change_pct"], expected_change_pct, places=6)

    def test_funding_rate_and_fear_greed_stay_none_end_to_end(self):
        n = 250
        candles = _flat(n)
        candles[125] = C(_ts(125), 102, 103, 99.5, 100.5)

        captured = []

        def fake_run_analysis(snapshot):
            captured.append(snapshot)
            return {"execution": {
                "decision": "TRADE", "direction": "LONG", "entry_price": 101.0,
                "stop_loss": 96.0, "tp1": 106.0, "tp2": 111.0, "confidence": 88, "rr_ratio": 3.1,
            }}

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {"decision": "TRADE", "direction": "LONG", "entry_price": 100.0,
                    "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0,
                    "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None}

        def fake_replay(*a, **k):
            for i, bar in enumerate(candles):
                yield {
                    "bar_index": i, "timestamp": bar["timestamp"],
                    "open_price": bar["open"], "high_price": bar["high"],
                    "low_price": bar["low"], "close_price": bar["close"],
                    "candles_1d": [], "candles_4h": [], "candles_1h": [],
                    "candles_15m": candles[max(0, i - 95):i + 1],
                    "smc_levels": {"key_levels": [], "confluence": {"score": 90, "factors": {"structure_1D": "bullish"}, "conflicts": []}},
                    "kill_zone": "london",
                }

        with patch.object(_settings, "anthropic_api_key", _REAL_ANTHROPIC_KEY), \
             patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay, "replay", side_effect=fake_replay), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=True), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal), \
             patch("app.services.claude_service.run_analysis", side_effect=fake_run_analysis):
            engine.run_backtest(
                "BTC/USDT",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                until=datetime(2026, 1, 2, tzinfo=timezone.utc),
                claude_sample_max=1,
            )

        self.assertEqual(len(captured), 1)
        snap = captured[0]
        self.assertIsNone(snap["funding_rate"])
        self.assertIsNone(snap["fear_greed_index"])
        ticker = snap["ticker"]
        self.assertIsNotNone(ticker["high"])
        self.assertIsNotNone(ticker["low"])
        self.assertIsNotNone(ticker["volume"])
        self.assertIsNotNone(ticker["change_pct"])


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics — engine.py
# ─────────────────────────────────────────────────────────────────────────────

class TestDiagnostics(unittest.TestCase):
    def _trade(self, entry, sl, qty, fees, slippage_cost, r_multiple, outcome="tp2",
               entry_time="t0", exit_time="t1"):
        return {
            "entry_price": entry, "stop_loss": sl, "qty": qty, "fees": fees,
            "slippage_cost": slippage_cost, "r_multiple": r_multiple, "outcome": outcome,
            "entry_time": entry_time, "exit_time": exit_time,
        }

    def test_sl_pct_stats_and_cost_r_stats_and_buckets(self):
        trades = [
            self._trade(100, 99.95, 2.0, 0.1, 0.05, 1.5),      # sl_pct=0.05% -> <0.1%
            self._trade(100, 99.8, 2.0, 0.2, 0.1, -1.0),        # sl_pct=0.2% -> 0.1-0.3%
            self._trade(100, 99.5, 2.0, 0.3, 0.1, 2.0),         # sl_pct=0.5% -> 0.3-0.6%
            self._trade(100, 99.0, 1.0, 0.05, 0.05, -1.0, outcome="sl", entry_time="t0", exit_time="t0"),  # 1.0% -> >=0.6%, fill-bar SL
        ]
        diag = engine._compute_diagnostics(trades)

        self.assertIsNotNone(diag["sl_pct_stats"])
        self.assertAlmostEqual(diag["sl_pct_stats"]["min"], 0.05, places=2)
        self.assertAlmostEqual(diag["sl_pct_stats"]["max"], 1.0, places=2)

        self.assertIsNotNone(diag["cost_r_stats"])
        self.assertIn("median", diag["cost_r_stats"])
        self.assertIn("mean", diag["cost_r_stats"])

        self.assertEqual(diag["fill_bar_sl_exits"], 1)

        buckets = diag["r_by_sl_bucket"]
        self.assertEqual(buckets["<0.1%"]["count"], 1)
        self.assertEqual(buckets["0.1-0.3%"]["count"], 1)
        self.assertEqual(buckets["0.3-0.6%"]["count"], 1)
        self.assertEqual(buckets[">=0.6%"]["count"], 1)
        self.assertAlmostEqual(buckets["<0.1%"]["expectancy_r"], 1.5, places=4)

    def test_empty_trades_gives_neutral_diagnostics(self):
        diag = engine._compute_diagnostics([])
        self.assertIsNone(diag["sl_pct_stats"])
        self.assertIsNone(diag["cost_r_stats"])
        self.assertEqual(diag["fill_bar_sl_exits"], 0)
        for label, stats in diag["r_by_sl_bucket"].items():
            self.assertEqual(stats["count"], 0)
            self.assertEqual(stats["expectancy_r"], 0.0)

    def test_diagnostics_json_serializable(self):
        trades = [
            self._trade(100, 99.95, 2.0, 0.1, 0.05, 1.5),
            self._trade(100, 99.0, 1.0, 0.05, 0.05, -1.0, outcome="sl", entry_time="t0", exit_time="t0"),
        ]
        diag = engine._compute_diagnostics(trades)
        json.dumps(diag)
        json.dumps(engine._compute_diagnostics([]))


# ─────────────────────────────────────────────────────────────────────────────
# Setup-key set — trade_simulator._matches_any_setup rewrite (section 7)
# ─────────────────────────────────────────────────────────────────────────────

class TestSetupKeySet(unittest.TestCase):
    def test_float_noise_within_1e10_still_matches_after_8dp_rounding(self):
        # 1e-10 noise is well inside the ~5e-9 tolerance 8dp rounding gives at
        # this price magnitude, so it must still hash to the same setup key.
        order = {"direction": "LONG", "entry_price": 100.0, "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0}
        noisy_sig = {
            "direction": "LONG",
            "entry_price": 100.0 + 1e-10,
            "stop_loss": 95.0 - 1e-10,
            "tp1": 105.0 + 1e-10,
            "tp2": 110.0,
        }
        self.assertEqual(trade_simulator._setup_key(order), trade_simulator._setup_key(noisy_sig))
        self.assertTrue(trade_simulator._matches_any_setup(noisy_sig, {trade_simulator._setup_key(order)}))

    def test_setup_reuse_blocking_still_works_end_to_end(self):
        # Same scenario as test_phase6_fidelity's setup-reentry tests, confirming the
        # set-based rewrite preserves behavior.
        n = 20
        candles = _flat(n)
        signals = [_long_signal(2), _long_signal(8)]  # identical setup, second after first cancels via ttl
        bias = {i: "bullish" for i in range(n)}
        r = trade_simulator.simulate(candles, signals, bias, SimConfig(order_ttl_bars=3))
        self.assertEqual(r["orders"]["placed"], 1)
        self.assertGreaterEqual(r["orders"]["setup_reused_blocked"], 1)


if __name__ == "__main__":
    unittest.main()
