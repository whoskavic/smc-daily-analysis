"""
Unit tests Phase 3 — claude_service.py (no real API calls)
"""
import sys, os, json, types, unittest
from unittest.mock import MagicMock, patch

# ── Setup path ────────────────────────────────────────────────────────────────
BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

# ── Stub config (no .env needed) ──────────────────────────────────────────────
# Build fake app package before import if needed
if "app" not in sys.modules:
    app_mod = types.ModuleType("app")
    app_mod.__path__ = [os.path.join(BACKEND, "app")]
    sys.modules["app"] = app_mod

if "app.config" not in sys.modules:
    cfg_mod = types.ModuleType("app.config")
    settings_stub = MagicMock()
    settings_stub.anthropic_api_key = "sk-test-stub"
    settings_stub.claude_model = "claude-sonnet-4-6"
    cfg_mod.settings = settings_stub
    sys.modules["app.config"] = cfg_mod

# Now safe to import from the real service file
from app.services.claude_service import (   # noqa: E402
    _parse_json_response,
    _validate_analysis,
    _no_trade_fallback,
    _to_legacy_result,
    build_prompt,
    run_analysis,
    AnalysisValidationError,
)
import anthropic


# ── Test data helpers ──────────────────────────────────────────────────────────

def _valid_trade_json():
    return {
        "institutional_bias": {
            "phase": "Manipulation",
            "direction": "bullish",
            "htf_bias": "bullish",
            "itf_bias": "bearish",
            "reasoning": "BTC HTF structure bullish. 4H CHoCH above OB confirms bias.",
        },
        "trap_analysis": {
            "type": "Bear Trap",
            "retail_sl_cluster": 64800.0,
            "sweep_target": 64650.0,
            "description": "Equal lows at 64800 — retail short SL ripe for sweep.",
        },
        "confluence": {
            "score": 88,
            "factors": {
                "market_structure": "bullish",
                "order_block": "bullish",
                "fvg": "bullish",
                "funding_rate": "neutral",
                "kill_zone": "bullish",
            },
            "conflicts": [],
        },
        "key_levels": [
            {
                "type": "Order Block Bullish",
                "price": 65200.0,
                "low": 65100.0,
                "high": 65300.0,
                "tf": "4H",
                "strength": 8,
            }
        ],
        "execution": {
            "decision": "TRADE",
            "direction": "LONG",
            "entry_type": "LIMIT",
            "entry_price": 65200.0,
            "entry_zone": [65100.0, 65300.0],
            "stop_loss": 64600.0,
            "tp1": 67200.0,
            "tp2": 69000.0,
            "tp1_close_pct": 50,
            "rr_ratio": 3.3,
            "confidence": 88,
            "invalidation": "Candle close below 64800 on 4H",
            "recommended_leverage": 5,
            "no_trade_reason": None,
            "wait_for": None,
        },
        "narrative": "BTC manipulasi area OB 4H bullish. Setup LONG dengan R:R 1:3.3.",
    }


def _valid_no_trade_json():
    return {
        "institutional_bias": {
            "phase": "Distribution",
            "direction": "bearish",
            "htf_bias": "bearish",
            "itf_bias": "bearish",
            "reasoning": "LH/LL structure on daily. Premium pricing.",
        },
        "trap_analysis": {
            "type": "None",
            "retail_sl_cluster": None,
            "sweep_target": None,
            "description": "No clear trap setup identified.",
        },
        "confluence": {
            "score": 42,
            "factors": {"market_structure": "bearish"},
            "conflicts": ["Funding rate contradicts bearish bias"],
        },
        "key_levels": [],
        "execution": {
            "decision": "NO_TRADE",
            "direction": None,
            "entry_type": None,
            "entry_price": None,
            "entry_zone": [None, None],
            "stop_loss": None,
            "tp1": None,
            "tp2": None,
            "tp1_close_pct": 50,
            "rr_ratio": None,
            "confidence": 42,
            "invalidation": None,
            "recommended_leverage": None,
            "no_trade_reason": "Confluence score terlalu rendah (42). Funding rate bertentangan.",
            "wait_for": None,
        },
        "narrative": "Struktur bearish namun confluence lemah. NO TRADE.",
    }


def _minimal_snapshot():
    return {
        "symbol": "BTC/USDT",
        "ticker": {"last": 65000, "high": 66000, "low": 64000, "volume": 1000, "change_pct": 1.5},
        "candles_1d": [
            {"timestamp": "2024-01-01", "open": 63000, "high": 64000, "low": 62000, "close": 63500, "volume": 100},
            {"timestamp": "2024-01-02", "open": 63500, "high": 65000, "low": 63000, "close": 64800, "volume": 120},
            {"timestamp": "2024-01-03", "open": 64800, "high": 66000, "low": 64500, "close": 65200, "volume": 115},
        ],
        "candles_4h": [],
        "candles_1h": [],
        "funding_rate": 0.0001,
        "fear_greed_index": 65,
    }


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestParseJsonResponse(unittest.TestCase):
    def test_valid_trade(self):
        result = _parse_json_response(json.dumps(_valid_trade_json()))
        self.assertEqual(result["execution"]["decision"], "TRADE")

    def test_valid_no_trade(self):
        result = _parse_json_response(json.dumps(_valid_no_trade_json()))
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")

    def test_strips_markdown_fences(self):
        wrapped = "```json\n" + json.dumps(_valid_no_trade_json()) + "\n```"
        result  = _parse_json_response(wrapped)
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")

    def test_finds_brace_in_prose(self):
        with_prose = "Here is the analysis:\n" + json.dumps(_valid_no_trade_json())
        result     = _parse_json_response(with_prose)
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")

    def test_raises_on_invalid_json(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_json_response("not json at all")

    def test_raises_on_schema_violation(self):
        data = _valid_trade_json()
        del data["execution"]["stop_loss"]
        with self.assertRaises(AnalysisValidationError):
            _parse_json_response(json.dumps(data))


class TestValidateAnalysis(unittest.TestCase):
    def test_valid_trade_passes(self):
        _validate_analysis(_valid_trade_json())

    def test_valid_no_trade_passes(self):
        _validate_analysis(_valid_no_trade_json())

    def test_missing_top_level_key(self):
        data = _valid_trade_json()
        del data["narrative"]
        with self.assertRaises(AnalysisValidationError) as ctx:
            _validate_analysis(data)
        self.assertIn("narrative", str(ctx.exception))

    def test_invalid_direction_enum(self):
        data = _valid_trade_json()
        data["institutional_bias"]["direction"] = "very_bullish"
        with self.assertRaises(AnalysisValidationError):
            _validate_analysis(data)

    def test_confluence_score_out_of_range(self):
        data = _valid_trade_json()
        data["confluence"]["score"] = 150
        with self.assertRaises(AnalysisValidationError):
            _validate_analysis(data)

    def test_trade_missing_sl(self):
        data = _valid_trade_json()
        data["execution"]["stop_loss"] = None
        with self.assertRaises(AnalysisValidationError) as ctx:
            _validate_analysis(data)
        self.assertIn("stop_loss", str(ctx.exception))

    def test_trade_rr_too_low(self):
        data = _valid_trade_json()
        data["execution"]["rr_ratio"] = 0.5
        with self.assertRaises(AnalysisValidationError):
            _validate_analysis(data)

    def test_no_trade_missing_reason(self):
        data = _valid_no_trade_json()
        data["execution"]["no_trade_reason"] = None
        with self.assertRaises(AnalysisValidationError):
            _validate_analysis(data)

    def test_invalid_decision_enum(self):
        data = _valid_trade_json()
        data["execution"]["decision"] = "MAYBE"
        with self.assertRaises(AnalysisValidationError):
            _validate_analysis(data)


class TestLegacyResultMapping(unittest.TestCase):
    def test_trade_result_all_legacy_keys(self):
        result = _to_legacy_result(_valid_trade_json(), _minimal_snapshot(), "prompt", "{}")
        # Scheduler DailyAnalysis keys
        for key in ["symbol", "bias", "confidence", "key_levels", "trade_idea",
                    "full_analysis", "raw_prompt", "trade_direction",
                    "trade_entry", "trade_sl", "trade_tp",
                    "open_price", "high_price", "low_price", "close_price",
                    "volume", "funding_rate", "fear_greed_index"]:
            self.assertIn(key, result, f"Missing key: {key}")

        self.assertEqual(result["bias"], "bullish")
        self.assertEqual(result["confidence"], 88)
        self.assertEqual(result["trade_direction"], "LONG")
        self.assertEqual(result["trade_entry"], 65200.0)
        self.assertEqual(result["trade_sl"], 64600.0)
        self.assertEqual(result["trade_tp"], 67200.0)

    def test_executor_keys_in_execution(self):
        result = _to_legacy_result(_valid_trade_json(), _minimal_snapshot(), "p", "{}")
        exec_d = result["execution"]
        for key in ["decision", "direction", "stop_loss", "tp1", "tp2",
                    "tp1_close_pct", "recommended_leverage", "symbol"]:
            self.assertIn(key, exec_d, f"Missing executor key: {key}")
        self.assertEqual(exec_d["symbol"], "BTC/USDT")

    def test_no_trade_direction_is_wait(self):
        result = _to_legacy_result(_valid_no_trade_json(), _minimal_snapshot(), "p", "{}")
        self.assertEqual(result["trade_direction"], "WAIT")
        self.assertIsNone(result["trade_entry"])

    def test_ohlcv_from_last_candle(self):
        result = _to_legacy_result(_valid_trade_json(), _minimal_snapshot(), "p", "{}")
        self.assertEqual(result["close_price"], 65200)
        self.assertEqual(result["high_price"], 66000)
        self.assertEqual(result["funding_rate"], 0.0001)
        self.assertEqual(result["fear_greed_index"], 65)


class TestNoTradeFallback(unittest.TestCase):
    def test_fallback_is_valid(self):
        fallback = _no_trade_fallback("ETH/USDT", "API timeout")
        _validate_analysis(fallback)  # must pass schema validation

    def test_decision_no_trade(self):
        fallback = _no_trade_fallback("SOL/USDT", "test")
        self.assertEqual(fallback["execution"]["decision"], "NO_TRADE")

    def test_confidence_zero(self):
        fallback = _no_trade_fallback("SOL/USDT", "test")
        self.assertEqual(fallback["execution"]["confidence"], 0)

    def test_reason_propagated(self):
        fallback = _no_trade_fallback("BTC/USDT", "ISP timeout error")
        self.assertIn("ISP timeout error", fallback["execution"]["no_trade_reason"])


class TestBuildPrompt(unittest.TestCase):
    def test_contains_symbol(self):
        self.assertIn("BTC/USDT", build_prompt(_minimal_snapshot()))

    def test_optional_fields_not_required(self):
        prompt = build_prompt(_minimal_snapshot())  # should not raise
        self.assertIsInstance(prompt, str)

    def test_kill_zone_in_prompt(self):
        snapshot = {**_minimal_snapshot(), "kill_zone": "NY_AM"}
        self.assertIn("NY_AM", build_prompt(snapshot))

    def test_smc_levels_in_prompt(self):
        snapshot = {**_minimal_snapshot(), "smc_levels": {"ob_bullish": {"price": 65000}}}
        self.assertIn("PRE-COMPUTED SMC LEVELS", build_prompt(snapshot))

    def test_15m_candles_included(self):
        snapshot = {
            **_minimal_snapshot(),
            "candles_15m": [{"timestamp": "2024-01-03T12:00", "open": 65000,
                              "high": 65100, "low": 64900, "close": 65050, "volume": 50}]
        }
        self.assertIn("15m", build_prompt(snapshot))

    def test_cvd_in_prompt(self):
        snapshot = {**_minimal_snapshot(), "cvd": 12345.67}
        self.assertIn("CVD", build_prompt(snapshot))


class TestRunAnalysisRetry(unittest.TestCase):
    def _make_msg(self, text):
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        return msg

    def test_success_first_attempt(self):
        with patch("app.services.claude_service._get_client") as mc:
            mc.return_value.messages.create.return_value = self._make_msg(
                json.dumps(_valid_trade_json()))
            result = run_analysis(_minimal_snapshot())
        self.assertEqual(result["execution"]["decision"], "TRADE")
        self.assertEqual(result["bias"], "bullish")
        self.assertEqual(mc.return_value.messages.create.call_count, 1)

    def test_retry_on_bad_json_then_success(self):
        valid = json.dumps(_valid_no_trade_json())
        with patch("app.services.claude_service._get_client") as mc:
            mc.return_value.messages.create.side_effect = [
                self._make_msg("not valid json"),
                self._make_msg(valid),
            ]
            result = run_analysis(_minimal_snapshot(), max_retries=1)
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        self.assertEqual(mc.return_value.messages.create.call_count, 2)

    def test_fallback_after_all_retries_fail(self):
        with patch("app.services.claude_service._get_client") as mc:
            mc.return_value.messages.create.return_value = self._make_msg("still not json")
            result = run_analysis(_minimal_snapshot(), max_retries=1)
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        self.assertEqual(result["bias"], "neutral")
        self.assertEqual(mc.return_value.messages.create.call_count, 2)

    def test_fallback_on_api_error(self):
        with patch("app.services.claude_service._get_client") as mc:
            mc.return_value.messages.create.side_effect = \
                anthropic.APIConnectionError(request=MagicMock())
            result = run_analysis(_minimal_snapshot())
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        # API errors don't retry
        self.assertEqual(mc.return_value.messages.create.call_count, 1)

    def test_retry_message_contains_correction_context(self):
        """Second call must include correction context in messages."""
        valid = json.dumps(_valid_trade_json())
        calls_messages = []
        def capture_call(**kwargs):
            calls_messages.append(kwargs["messages"])
            if len(calls_messages) == 1:
                return self._make_msg("bad json")
            return self._make_msg(valid)

        with patch("app.services.claude_service._get_client") as mc:
            mc.return_value.messages.create.side_effect = \
                lambda **kw: capture_call(**kw)
            run_analysis(_minimal_snapshot(), max_retries=1)

        # Second call must have 3 messages (user, assistant, correction)
        self.assertEqual(len(calls_messages[1]), 3)
        # Third message should be the correction
        self.assertIn("tidak valid", calls_messages[1][2]["content"])


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    res    = runner.run(
        unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    )
    print(f"\n{'='*55}")
    print(f"Tests run: {res.testsRun}  |  Failures: {len(res.failures)}  |  Errors: {len(res.errors)}")
    print(f"{'='*55}")
    sys.exit(0 if res.wasSuccessful() else 1)
