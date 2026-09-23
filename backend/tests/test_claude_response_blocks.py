"""
Unit tests — claude_service.py response-block extraction (no real API calls).

With CLAUDE_MODEL set to a thinking model (e.g. claude-sonnet-5), the API
returns a ThinkingBlock as content[0], so `message.content[0].text` raises
AttributeError (ThinkingBlock has no .text) and run_analysis silently falls
back to NO_TRADE with an opaque "attempt(s): 'ThinkingBlock' object has no
attribute 'text'" reason. This covers the fix: _extract_text_block() finds
the first block whose .type == "text" wherever it sits, raises a ValueError
naming the block types when none is found, and an explicit max_tokens
truncation check runs before JSON parsing is attempted.

Uses plain stub objects (not anthropic's real block/message types) so these
run without a real API key or network access.
"""
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

# ── Stub config (no .env needed) — mirrors test_phase3.py ──────────────────
if "app" not in sys.modules:
    app_mod = types.ModuleType("app")
    app_mod.__path__ = [os.path.join(BACKEND, "app")]
    sys.modules["app"] = app_mod

if "app.config" not in sys.modules:
    cfg_mod = types.ModuleType("app.config")
    settings_stub = MagicMock()
    settings_stub.anthropic_api_key = "sk-test-stub"
    settings_stub.claude_model = "claude-sonnet-5"
    cfg_mod.settings = settings_stub
    sys.modules["app.config"] = cfg_mod

from app.services.claude_service import run_analysis, _extract_text_block  # noqa: E402


# ── Plain stub objects — no anthropic types imported ────────────────────────

class _TextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _ThinkingBlock:
    """Real anthropic.types.ThinkingBlock has .thinking, not .text — that
    absence is exactly what raised the original AttributeError."""
    def __init__(self, thinking="reasoning about market structure..."):
        self.type = "thinking"
        self.thinking = thinking


class _Message:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


# ── Test data helpers ────────────────────────────────────────────────────────

def _valid_trade_json():
    return {
        "institutional_bias": {
            "phase": "Manipulation", "direction": "bullish",
            "htf_bias": "bullish", "itf_bias": "bullish",
            "reasoning": "HTF structure bullish, OB confirms.",
        },
        "trap_analysis": {
            "type": "None", "retail_sl_cluster": None, "sweep_target": None,
            "description": "No trap identified.",
        },
        "confluence": {
            "score": 88, "factors": {"market_structure": "bullish"}, "conflicts": [],
        },
        "key_levels": [],
        "execution": {
            "decision": "TRADE", "direction": "LONG", "entry_type": "LIMIT",
            "entry_price": 65200.0, "entry_zone": [65100.0, 65300.0],
            "stop_loss": 64600.0, "tp1": 67200.0, "tp2": 69000.0,
            "tp1_close_pct": 50, "rr_ratio": 3.3, "confidence": 88,
            "invalidation": "Close below 64600 on 4H", "recommended_leverage": 5,
            "no_trade_reason": None, "wait_for": None,
        },
        "narrative": "LONG setup, R:R 1:3.3.",
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
        "candles_4h": [], "candles_1h": [],
        "funding_rate": 0.0001, "fear_greed_index": 65,
    }


def _run_with_message(message, max_retries=1):
    with patch("app.services.claude_service._get_client") as mc:
        mc.return_value.messages.create.return_value = message
        return run_analysis(_minimal_snapshot(), max_retries=max_retries)


# ── _extract_text_block unit tests ───────────────────────────────────────────

class TestExtractTextBlock(unittest.TestCase):
    def test_text_only(self):
        msg = _Message([_TextBlock("hello")])
        self.assertEqual(_extract_text_block(msg), "hello")

    def test_thinking_then_text_is_found(self):
        msg = _Message([_ThinkingBlock(), _TextBlock("the json body")])
        self.assertEqual(_extract_text_block(msg), "the json body")

    def test_text_block_found_even_when_not_first(self):
        # same shape as thinking-then-text, phrased as its own guarantee:
        # extraction scans the whole list, it doesn't assume position 0.
        msg = _Message([_ThinkingBlock(), _ThinkingBlock(), _TextBlock("payload")])
        self.assertEqual(_extract_text_block(msg), "payload")

    def test_thinking_only_raises_value_error_listing_block_types(self):
        msg = _Message([_ThinkingBlock()])
        with self.assertRaises(ValueError) as ctx:
            _extract_text_block(msg)
        self.assertIn("thinking", str(ctx.exception))
        self.assertIn("no text block in response", str(ctx.exception))


# ── run_analysis end-to-end behavior ─────────────────────────────────────────

class TestRunAnalysisResponseBlocks(unittest.TestCase):
    def test_thinking_then_text_parses_same_as_text_only(self):
        trade_json = json.dumps(_valid_trade_json())

        result_with_thinking = _run_with_message(_Message([_ThinkingBlock(), _TextBlock(trade_json)]))
        result_text_only = _run_with_message(_Message([_TextBlock(trade_json)]))

        self.assertEqual(result_with_thinking["execution"]["decision"], "TRADE")
        self.assertEqual(result_with_thinking["execution"], result_text_only["execution"])
        self.assertEqual(result_with_thinking["bias"], result_text_only["bias"])

    def test_text_only_unchanged(self):
        trade_json = json.dumps(_valid_trade_json())
        result = _run_with_message(_Message([_TextBlock(trade_json)]))
        self.assertEqual(result["execution"]["decision"], "TRADE")
        self.assertEqual(result["execution"]["entry_price"], 65200.0)

    def test_thinking_only_falls_back_with_meaningful_reason_not_attribute_error(self):
        result = _run_with_message(_Message([_ThinkingBlock()]))

        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        reason = result["execution"]["no_trade_reason"]
        self.assertIn("no text block in response", reason)
        self.assertIn("thinking", reason)
        self.assertNotIn("AttributeError", reason)
        self.assertNotIn("has no attribute", reason)

    def test_max_tokens_truncation_raises_and_reaches_fallback(self):
        # Otherwise-parseable body, but stop_reason says it was cut off —
        # the truncation check must fire before _parse_json_response runs.
        trade_json = json.dumps(_valid_trade_json())
        result = _run_with_message(_Message([_TextBlock(trade_json)], stop_reason="max_tokens"))

        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        reason = result["execution"]["no_trade_reason"]
        self.assertIn("truncated at max_tokens", reason)

    def test_non_max_tokens_stop_reason_does_not_trigger_truncation_check(self):
        trade_json = json.dumps(_valid_trade_json())
        result = _run_with_message(_Message([_TextBlock(trade_json)], stop_reason="end_turn"))
        self.assertEqual(result["execution"]["decision"], "TRADE")


if __name__ == "__main__":
    unittest.main()
