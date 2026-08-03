"""
Unit tests Phase 2 — smc_engine.py (pure functions, no I/O, no API calls).
"""
import sys
import os
import unittest

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

from app.services import smc_engine as engine  # noqa: E402


def C(o, h, l, c, v=100):
    """Shorthand OHLCV candle dict matching binance_service.fetch_ohlcv shape."""
    return {"timestamp": "2026-01-01T00:00:00+00:00", "open": o, "high": h, "low": l, "close": c, "volume": v}


class TestSwingDetection(unittest.TestCase):
    def test_single_peak_detected(self):
        candles = [C(9, 10, 8, 9), C(11, 12, 10, 11), C(14, 15, 13, 14), C(11, 12, 10, 11), C(9, 10, 8, 9)]
        swings = engine.detect_swings(candles, lookback=2)
        self.assertEqual(len(swings), 1)
        self.assertEqual(swings[0], {"index": 2, "type": "high", "price": 15.0, "timestamp": "2026-01-01T00:00:00+00:00"})

    def test_insufficient_candles_returns_empty(self):
        candles = [C(1, 2, 0, 1), C(1, 2, 0, 1)]
        self.assertEqual(engine.detect_swings(candles, lookback=2), [])

    def test_flat_tie_is_not_a_swing(self):
        # Two equal highs tied for the max within the window — neither is a unique extreme.
        candles = [C(1, 10, 0, 1), C(1, 5, 0, 1), C(1, 10, 0, 1)]
        swings = engine.detect_swings(candles, lookback=1)
        self.assertEqual([s for s in swings if s["type"] == "high"], [])


class TestFairValueGaps(unittest.TestCase):
    def test_bullish_gap(self):
        candles = [C(10, 12, 8, 11), C(15, 20, 14, 19), C(22, 25, 21, 24)]
        fvgs = engine.detect_fair_value_gaps(candles)
        self.assertEqual(len(fvgs), 1)
        self.assertEqual(fvgs[0]["type"], "Fair Value Gap Bullish")
        self.assertEqual(fvgs[0]["low"], 12)
        self.assertEqual(fvgs[0]["high"], 21)

    def test_bearish_gap(self):
        candles = [C(24, 25, 21, 22), C(19, 20, 14, 15), C(11, 12, 8, 10)]
        fvgs = engine.detect_fair_value_gaps(candles)
        self.assertEqual(len(fvgs), 1)
        self.assertEqual(fvgs[0]["type"], "Fair Value Gap Bearish")
        self.assertEqual(fvgs[0]["low"], 12)
        self.assertEqual(fvgs[0]["high"], 21)

    def test_no_gap_when_candles_overlap(self):
        candles = [C(10, 12, 8, 11), C(11, 13, 9, 12), C(12, 14, 10, 13)]
        self.assertEqual(engine.detect_fair_value_gaps(candles), [])


class TestBosChochAndOrderBlocks(unittest.TestCase):
    def _uptrend_break_candles(self):
        return [
            C(100, 102, 98, 101),
            C(101, 103, 99, 100),   # swing high @ 103
            C(100, 101, 95, 96),
            C(96, 100, 95, 99),
            C(99, 108, 98, 107),
            C(107, 108, 100, 101),
            C(101, 102, 99, 100),
            C(100, 120, 99, 119),   # close breaks above 103 -> BOS bullish
        ]

    def test_bos_detected_on_structure_break(self):
        candles = self._uptrend_break_candles()
        swings = engine.detect_swings(candles, lookback=1)
        bos = engine.detect_bos_choch(candles, swings, tf="1H")
        self.assertEqual(len(bos), 1)
        self.assertEqual(bos[0]["type"], "BOS")
        self.assertEqual(bos[0]["direction"], "bullish")
        self.assertEqual(bos[0]["price"], 103.0)
        self.assertEqual(bos[0]["index"], 4)

    def test_first_break_is_bos_not_choch(self):
        # No established trend yet — the very first structure break is BOS, never CHoCH.
        candles = self._uptrend_break_candles()
        swings = engine.detect_swings(candles, lookback=1)
        bos = engine.detect_bos_choch(candles, swings, tf="1H")
        self.assertTrue(all(e["type"] == "BOS" for e in bos))

    def test_choch_on_trend_reversal(self):
        # Establish bullish trend, then break the low below the trend origin -> CHoCH bearish.
        candles = [
            C(50, 52, 48, 51),
            C(51, 61, 50, 59),    # swing high @ 61
            C(59, 60, 39, 41),    # swing low @ 39
            C(41, 63, 40, 62),    # close breaks 61 -> BOS bullish, trend=bullish
            C(62, 70, 29, 31),    # close breaks below 39 -> CHoCH bearish
        ]
        swings = engine.detect_swings(candles, lookback=1)
        bos = engine.detect_bos_choch(candles, swings, tf="1H")
        types = [(e["type"], e["direction"]) for e in bos]
        self.assertIn(("BOS", "bullish"), types)
        self.assertIn(("CHoCH", "bearish"), types)

    def test_order_block_is_last_opposite_candle_before_break(self):
        candles = self._uptrend_break_candles()
        swings = engine.detect_swings(candles, lookback=1)
        bos = engine.detect_bos_choch(candles, swings, tf="1H")
        obs = engine.detect_order_blocks(candles, bos, tf="1H")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["type"], "Order Block Bullish")
        self.assertEqual(obs[0]["low"], 95)
        self.assertEqual(obs[0]["high"], 101)


class TestLiquidityZones(unittest.TestCase):
    def test_equal_highs_clustered(self):
        candles = [
            C(10, 15, 9, 10),
            C(10, 12, 9, 10),
            C(10, 15.01, 9, 10),
            C(10, 11, 9, 10),
            C(10, 15.02, 9, 10),
            C(10, 11, 9, 10),
        ]
        swings = engine.detect_swings(candles, lookback=1)
        zones = engine.detect_liquidity_zones(candles, swings)
        equal_highs = [z for z in zones if z["type"] == "Equal Highs"]
        self.assertEqual(len(equal_highs), 1)
        self.assertAlmostEqual(equal_highs[0]["low"], 15.01)
        self.assertAlmostEqual(equal_highs[0]["high"], 15.02)

    def test_no_cluster_below_two_points(self):
        candles = [C(10, 15, 9, 10), C(10, 11, 9, 10), C(10, 20, 9, 10)]
        swings = engine.detect_swings(candles, lookback=1)
        self.assertEqual(engine.detect_liquidity_zones(candles, swings), [])


class TestPremiumDiscount(unittest.TestCase):
    def test_premium_zone_above_equilibrium(self):
        candles = [C(10, 20, 5, 10), C(10, 15, 8, 18)]
        pd = engine.premium_discount_zone(candles)
        self.assertEqual(pd["equilibrium"], 12.5)
        self.assertEqual(pd["zone"], "premium")

    def test_discount_zone_below_equilibrium(self):
        candles = [C(10, 20, 5, 10), C(10, 15, 6, 7)]
        pd = engine.premium_discount_zone(candles)
        self.assertEqual(pd["zone"], "discount")

    def test_premium_discount_levels_shape(self):
        candles = [C(10, 20, 5, 10), C(10, 15, 8, 18)]
        levels = engine.premium_discount_levels(candles, tf="1D")
        self.assertEqual(len(levels), 1)
        self.assertEqual(levels[0]["type"], "Premium Zone")
        self.assertEqual(levels[0]["tf"], "1D")

    def test_empty_candles_returns_no_levels(self):
        self.assertEqual(engine.premium_discount_levels([]), [])


class TestConfluenceScoring(unittest.TestCase):
    def _htf_bullish_candles(self):
        return [C(100, 120, 99, 119), C(119, 125, 118, 124), C(124, 130, 123, 129)]

    def _ltf_bearish_candles(self):
        return [C(129, 130, 100, 101), C(101, 105, 99, 102), C(102, 104, 98, 100)]

    def test_htf_alignment_scores_high_despite_ltf_conflict(self):
        result = engine.score_confluence({
            "1D": self._htf_bullish_candles(),
            "4H": self._htf_bullish_candles(),
            "1H": self._ltf_bearish_candles(),
        })
        self.assertEqual(result["score"], 78)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertIn("1H", result["conflicts"][0])

    def test_full_alignment_scores_100(self):
        result = engine.score_confluence({
            "1D": self._htf_bullish_candles(),
            "4H": self._htf_bullish_candles(),
        })
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["conflicts"], [])

    def test_no_data_returns_zero(self):
        result = engine.score_confluence({"1D": [], "4H": []})
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["conflicts"], ["No candle data"])


class TestPreFilter(unittest.TestCase):
    def test_true_when_structural_type_present_and_score_high(self):
        levels = {"key_levels": [{"type": "BOS"}], "confluence": {"score": 50}}
        self.assertTrue(engine.has_structural_setup(levels))

    def test_false_when_no_structural_levels(self):
        levels = {"key_levels": [], "confluence": {"score": 90}}
        self.assertFalse(engine.has_structural_setup(levels))

    def test_false_when_score_too_low(self):
        levels = {"key_levels": [{"type": "BOS"}], "confluence": {"score": 10}}
        self.assertFalse(engine.has_structural_setup(levels))

    def test_non_structural_types_dont_count(self):
        levels = {"key_levels": [{"type": "Equal Highs"}], "confluence": {"score": 90}}
        self.assertFalse(engine.has_structural_setup(levels))


class TestBuildSmcLevels(unittest.TestCase):
    def test_orchestrator_merges_all_timeframes_and_matches_key_levels_schema(self):
        candles_by_tf = {
            "1D": [C(100, 120, 99, 119), C(119, 125, 118, 124), C(124, 130, 123, 129)],
            "4H": [C(100, 120, 99, 119), C(119, 125, 118, 124), C(124, 130, 123, 129)],
            "1H": [C(129, 130, 100, 101), C(101, 105, 99, 102), C(102, 104, 98, 100)],
            "15m": [],
        }
        result = engine.build_smc_levels(candles_by_tf)

        self.assertIn("key_levels", result)
        self.assertIn("confluence", result)
        self.assertGreater(len(result["key_levels"]), 0)

        required_keys = {"type", "price", "low", "high", "tf", "strength"}
        for level in result["key_levels"]:
            self.assertEqual(required_keys, set(level.keys()))
            self.assertIn(level["tf"], ("1D", "4H", "1H", "15m"))
            self.assertTrue(1 <= level["strength"] <= 10)

        self.assertEqual(result["confluence"]["score"], 78)
        self.assertTrue(engine.has_structural_setup(result))

    def test_empty_input_produces_empty_levels(self):
        result = engine.build_smc_levels({"1D": [], "4H": [], "1H": [], "15m": []})
        self.assertEqual(result["key_levels"], [])
        self.assertEqual(result["confluence"]["score"], 0)
        self.assertFalse(engine.has_structural_setup(result))


if __name__ == "__main__":
    unittest.main()
