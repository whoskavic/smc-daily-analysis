"""
Unit tests — smc_engine.SmcConfig (break_mode/ob_max_scan), its plumbing
through the backtest, and app/scripts/run_experiments.py's stats helpers.

Golden fixtures (tests/fixtures/golden_*.json) were generated from an
UNMODIFIED master (see tests/fixtures/generate_golden_fixtures.py) — these
tests assert the post-SmcConfig code, called with default SmcConfig() (or
no config argument at all, exercising the same default), reproduces them
exactly. That's the safety net for smc_engine.py being a protected file.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# ── Stub config (no .env needed) — mirrors the other test_phase6_*.py files ──
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
_settings.claude_model = "claude-sonnet-5"
_settings.binance_api_key = ""
_settings.binance_api_secret = ""
_settings.database_url = f"sqlite:///{_TMP_DB.name}"
_settings.risk_per_trade_pct = 1.0
_settings.max_leverage = 10
_settings.timezone = "Asia/Jakarta"
_settings.daily_analysis_time = "08:00"

from app.models.database import init_db  # noqa: E402
from app.services import smc_engine  # noqa: E402
from app.services.smc_engine import SmcConfig  # noqa: E402
from app.services.backtest import engine  # noqa: E402
from app.scripts import run_experiments as experiments  # noqa: E402

init_db()


def C(ts, o, h, l, c, v=100.0):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _ts(i, step_minutes=15, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
    return (start + timedelta(minutes=i * step_minutes)).isoformat()


def _flat(n, price=102.0, step_minutes=15):
    return [C(_ts(i, step_minutes), price, price + 1, price - 1, price) for i in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Golden fixtures — default behavior byte-identical to unmodified master
# ─────────────────────────────────────────────────────────────────────────────

class TestGoldenFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(FIXTURES_DIR, "golden_candles.json")) as f:
            cls.candles_by_tf = json.load(f)
        cls.candles_1h = cls.candles_by_tf["1H"]

    def _golden(self, name):
        with open(os.path.join(FIXTURES_DIR, name)) as f:
            return json.load(f)

    def test_detect_bos_choch_matches_golden(self):
        actual = smc_engine.detect_bos_choch(self.candles_1h, tf="1H")
        self.assertEqual(actual, self._golden("golden_bos_choch.json"))

    def test_detect_order_blocks_matches_golden(self):
        actual = smc_engine.detect_order_blocks(self.candles_1h, tf="1H")
        self.assertEqual(actual, self._golden("golden_order_blocks.json"))

    def test_tf_bias_matches_golden(self):
        actual = {"bias": smc_engine._tf_bias(self.candles_1h)}
        self.assertEqual(actual, self._golden("golden_tf_bias.json"))

    def test_score_confluence_matches_golden(self):
        actual = smc_engine.score_confluence(self.candles_by_tf)
        self.assertEqual(actual, self._golden("golden_confluence.json"))

    def test_build_smc_levels_matches_golden(self):
        actual = smc_engine.build_smc_levels(self.candles_by_tf)
        self.assertEqual(actual, self._golden("golden_smc_levels.json"))

    def test_explicit_default_smc_config_still_matches_golden(self):
        """Passing SmcConfig() explicitly must be identical to omitting it."""
        actual = smc_engine.build_smc_levels(self.candles_by_tf, config=SmcConfig())
        self.assertEqual(actual, self._golden("golden_smc_levels.json"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. break_mode: close vs wick disagree on a hand-built candle set
# ─────────────────────────────────────────────────────────────────────────────

class TestBreakMode(unittest.TestCase):
    def test_wick_detects_break_close_mode_does_not(self):
        # Swing high forms at index 2 (price=105, unique local max over the
        # +/-2 lookback window). Candle 5's high (106) pierces it, but its
        # close (101) doesn't confirm the break.
        candles = [
            C(_ts(0), 100, 101, 99, 100),
            C(_ts(1), 100, 102, 99, 101),
            C(_ts(2), 101, 105, 100, 102),
            C(_ts(3), 102, 103, 100, 101),
            C(_ts(4), 101, 102, 99, 100),
            C(_ts(5), 100, 106, 99, 101),   # wick > 105, close < 105
            C(_ts(6), 101, 103, 99, 100),
        ]

        close_events = smc_engine.detect_bos_choch(candles, tf="1H", config=SmcConfig(break_mode="close"))
        wick_events = smc_engine.detect_bos_choch(candles, tf="1H", config=SmcConfig(break_mode="wick"))

        close_breaks_105 = [e for e in close_events if e["price"] == 105]
        wick_breaks_105 = [e for e in wick_events if e["price"] == 105]

        self.assertEqual(close_breaks_105, [])
        self.assertEqual(len(wick_breaks_105), 1)
        self.assertEqual(wick_breaks_105[0]["index"], 5)
        self.assertEqual(wick_breaks_105[0]["direction"], "bullish")

    def test_default_break_mode_is_close(self):
        self.assertEqual(SmcConfig().break_mode, "close")


# ─────────────────────────────────────────────────────────────────────────────
# 3. ob_max_scan limits the backward order-block search
# ─────────────────────────────────────────────────────────────────────────────

class TestObMaxScan(unittest.TestCase):
    def test_small_scan_excludes_an_older_order_block_none_includes_it(self):
        # Only candle 0 is down-close (open > close) — everything else in
        # between is up-close, so the order block for a bullish break at
        # index 11 is 11 candles back.
        candles = [C(_ts(0), 100, 101, 99, 98)]
        for i in range(1, 12):
            candles.append(C(_ts(i), 100, 105, 99, 104))
        bos_events = [{"index": 11, "direction": "bullish", "strength": 5}]

        obs_unbounded = smc_engine.detect_order_blocks(
            candles, bos_events, tf="1H", config=SmcConfig(ob_max_scan=None),
        )
        obs_capped = smc_engine.detect_order_blocks(
            candles, bos_events, tf="1H", config=SmcConfig(ob_max_scan=5),
        )

        self.assertEqual(len(obs_unbounded), 1)
        self.assertEqual(obs_unbounded[0]["low"], 99)
        self.assertEqual(obs_unbounded[0]["high"], 101)
        self.assertEqual(obs_capped, [])

    def test_default_ob_max_scan_is_none(self):
        self.assertIsNone(SmcConfig().ob_max_scan)


# ─────────────────────────────────────────────────────────────────────────────
# 4. SmcConfig is frozen
# ─────────────────────────────────────────────────────────────────────────────

class TestSmcConfigFrozen(unittest.TestCase):
    def test_mutation_raises(self):
        cfg = SmcConfig()
        with self.assertRaises(FrozenInstanceError):
            cfg.break_mode = "wick"
        with self.assertRaises(FrozenInstanceError):
            cfg.ob_max_scan = 10


# ─────────────────────────────────────────────────────────────────────────────
# 5. run_backtest threads smc_config through to smc_engine and records it
# ─────────────────────────────────────────────────────────────────────────────

class TestRunBacktestSmcConfig(unittest.TestCase):
    def test_smc_config_reaches_build_smc_levels_changes_output_and_is_recorded(self):
        n = 320  # >= MIN_BARS_REQUIRED(200) + fallback warmup(120)
        candles = _flat(n, price=102.0)

        captured_configs = []

        def fake_build_smc_levels(candles_by_tf, config=SmcConfig()):
            captured_configs.append(config)
            if config.break_mode == "wick":
                return {
                    "key_levels": [{"type": "BOS", "price": 102.0, "low": None, "high": None, "tf": "1H", "strength": 5}],
                    "confluence": {"score": 90, "factors": {"structure_1D": "bullish"}, "conflicts": []},
                }
            return {"key_levels": [], "confluence": {"score": 0, "factors": {}, "conflicts": []}}

        def fake_rule_based_signal(smc_levels, current_price, **kwargs):
            return {
                "decision": "TRADE", "direction": "LONG", "entry_price": 102.0,
                "stop_loss": 98.0, "tp1": 106.0, "tp2": 110.0,
                "confidence": 90, "rr_ratio": 3.0, "no_trade_reason": None,
            }

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_replay.smc_engine, "build_smc_levels", side_effect=fake_build_smc_levels), \
             patch.object(engine.signal_simulator, "rule_based_signal", side_effect=fake_rule_based_signal):
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            until = datetime(2026, 1, 4, tzinfo=timezone.utc)
            result_default = engine.run_backtest("BTC/USDT", since=since, until=until, smc_config=None)
            result_wick = engine.run_backtest(
                "BTC/USDT", since=since, until=until,
                smc_config=SmcConfig(break_mode="wick", ob_max_scan=10),
            )

        # default config (no structural setup, per the fake) -> no trades
        self.assertEqual(result_default["total_trades"], 0)
        # wick config (structural setup present, per the fake) -> at least one trade
        self.assertGreater(result_wick["total_trades"], 0)

        # smc_config recorded in the result, both branches
        self.assertEqual(result_default["smc_config"], {"break_mode": "close", "ob_max_scan": None})
        self.assertEqual(result_wick["smc_config"], {"break_mode": "wick", "ob_max_scan": 10})

        # config genuinely reached build_smc_levels for both runs
        self.assertTrue(any(c.break_mode == "close" for c in captured_configs))
        self.assertTrue(any(c.break_mode == "wick" and c.ob_max_scan == 10 for c in captured_configs))

    def test_default_smc_config_recorded_when_omitted(self):
        n = 320
        candles = _flat(n)
        with patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=False):
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            until = datetime(2026, 1, 4, tzinfo=timezone.utc)
            result = engine.run_backtest("BTC/USDT", since=since, until=until)
        self.assertEqual(result["smc_config"], {"break_mode": "close", "ob_max_scan": None})


# ─────────────────────────────────────────────────────────────────────────────
# 6. run_experiments.py stats helpers — deterministic, json-dumpable
# ─────────────────────────────────────────────────────────────────────────────

class TestExperimentRunnerStatsHelpers(unittest.TestCase):
    def test_quarter_folds_deterministic_and_json_dumpable(self):
        since = datetime(2024, 9, 1, tzinfo=timezone.utc)
        until = datetime(2026, 9, 1, tzinfo=timezone.utc)
        folds_a = experiments.quarter_folds(since, until)
        folds_b = experiments.quarter_folds(since, until)
        self.assertEqual(folds_a, folds_b)
        json.dumps(folds_a)
        self.assertEqual(len(folds_a), 9)
        self.assertEqual(folds_a[0]["start"], since.isoformat())
        self.assertEqual(folds_a[-1]["end"], until.isoformat())

    def test_bootstrap_ci_deterministic_under_fixed_seed(self):
        diffs = [0.1, -0.2, 0.05, 0.3, -0.1, 0.0, 0.15]
        ci_a = experiments.bootstrap_ci(diffs, 500, seed=123)
        ci_b = experiments.bootstrap_ci(diffs, 500, seed=123)
        self.assertEqual(ci_a, ci_b)
        json.dumps(ci_a)
        self.assertIsNotNone(ci_a["ci_low"])
        self.assertLessEqual(ci_a["ci_low"], ci_a["ci_high"])

    def test_bootstrap_ci_different_seed_can_differ(self):
        diffs = [0.1, -0.2, 0.05, 0.3, -0.1, 0.0, 0.15, 0.22, -0.05]
        ci_a = experiments.bootstrap_ci(diffs, 500, seed=1)
        ci_b = experiments.bootstrap_ci(diffs, 500, seed=2)
        # point_estimate is seed-independent (plain mean); the CI bounds
        # are resampled and may differ between seeds.
        self.assertEqual(ci_a["point_estimate"], ci_b["point_estimate"])

    def test_bootstrap_ci_empty_diffs_handled_and_json_dumpable(self):
        ci = experiments.bootstrap_ci([], 100, seed=1)
        self.assertIsNone(ci["point_estimate"])
        self.assertIsNone(ci["ci_low"])
        json.dumps(ci)

    def test_paired_fold_diffs_skips_unpaired_folds(self):
        variant_folds = [
            {"start": "a", "end": "b", "trade_count": 3, "expectancy_r": 0.2},
            {"start": "b", "end": "c", "trade_count": 0, "expectancy_r": None},
        ]
        baseline_folds = [
            {"start": "a", "end": "b", "trade_count": 2, "expectancy_r": 0.1},
            {"start": "b", "end": "c", "trade_count": 4, "expectancy_r": -0.05},
        ]
        diffs = experiments.paired_fold_diffs(variant_folds, baseline_folds)
        self.assertEqual(diffs, [0.2 - 0.1])

    def test_evaluate_criteria_json_dumpable(self):
        variant = {"expectancy_r": 0.1, "total_trades": 50, "max_drawdown_pct": 10.0,
                   "fold_stats": [{"expectancy_r": 0.3}, {"expectancy_r": 0.1}]}
        baseline = {"expectancy_r": -0.05, "total_trades": 40, "max_drawdown_pct": 12.0,
                    "fold_stats": [{"expectancy_r": -0.1}, {"expectancy_r": 0.0}]}
        ci = {"ci_low": 0.05, "ci_high": 0.4, "point_estimate": 0.2, "n_folds": 2, "n_resamples": 100}
        criteria = experiments.evaluate_criteria(variant, baseline, ci)
        json.dumps(criteria)
        self.assertTrue(criteria["verdict_change_default"])


# ─────────────────────────────────────────────────────────────────────────────
# 7. run_experiments' data fetch is patchable — no network
# ─────────────────────────────────────────────────────────────────────────────

class TestExperimentRunnerNoNetwork(unittest.TestCase):
    def test_run_variant_fetch_is_patchable(self):
        n = 320
        candles = _flat(n)
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        until = datetime(2026, 1, 4, tzinfo=timezone.utc)

        with patch.object(engine.data_loader, "fetch_historical_ohlcv", return_value=candles), \
             patch.object(engine.smc_engine, "has_structural_setup", return_value=False):
            result = experiments.run_variant("BTC/USDT", since, until, SmcConfig())

        self.assertIn("smc_config", result)
        self.assertEqual(result["smc_config"], {"break_mode": "close", "ob_max_scan": None})

    def test_variant_report_and_bucket_trades_by_fold(self):
        folds = [
            {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-02T00:00:00+00:00"},
            {"start": "2026-01-02T00:00:00+00:00", "end": "2026-01-03T00:00:00+00:00"},
        ]
        trades = [
            {"entry_time": "2026-01-01T01:00:00+00:00", "r_multiple": 1.0},
            {"entry_time": "2026-01-01T05:00:00+00:00", "r_multiple": -0.5},
            {"entry_time": "2026-01-02T02:00:00+00:00", "r_multiple": 2.0},
        ]
        fold_stats = experiments.bucket_trades_by_fold(trades, folds)
        self.assertEqual(fold_stats[0]["trade_count"], 2)
        self.assertAlmostEqual(fold_stats[0]["expectancy_r"], 0.25)
        self.assertEqual(fold_stats[1]["trade_count"], 1)
        self.assertAlmostEqual(fold_stats[1]["expectancy_r"], 2.0)
        json.dumps(fold_stats)


if __name__ == "__main__":
    unittest.main()
