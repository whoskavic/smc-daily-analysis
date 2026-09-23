"""
Unit tests — claude_service._parse_json_response's near-JSON repair pass
(no real API calls).

In a 50-sample calibration run against claude-sonnet-5, roughly 20-30% of
calls failed strict json.loads with a trailing-comma or JS-style-comment
error and only succeeded on the API retry — doubling latency/cost for those
calls, and risking a silent NO_TRADE in live daily analysis on a second
failure. This covers the fix: a local, string-literal-aware repair pass
(trailing commas before }/], and // or /* */ comments, both only OUTSIDE
string literals) tried once after a strict parse failure, with the
ORIGINAL error re-raised unchanged if the repair doesn't help either.
"""
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

# ── Stub config (no .env needed) — mirrors test_claude_response_blocks.py ──
if "app" not in sys.modules:
    app_mod = types.ModuleType("app")
    app_mod.__path__ = [os.path.join(BACKEND, "app")]
    sys.modules["app"] = app_mod

if "app.config" not in sys.modules:
    cfg_mod = types.ModuleType("app.config")
    settings_stub = MagicMock()
    settings_stub.anthropic_api_key = "sk-test-stub"
    settings_stub.claude_model = "claude-sonnet-5"
    settings_stub.claude_max_tokens = 16000
    cfg_mod.settings = settings_stub
    sys.modules["app.config"] = cfg_mod

from app.services.claude_service import _parse_json_response, run_analysis  # noqa: E402


# ── Fixtures — hand-written JSON text (not json.dumps, so we control the
# exact byte-for-byte syntax: trailing commas, comments, escapes) ──────────

def _valid_no_trade_json_text() -> str:
    return """{
  "institutional_bias": {"phase": "Distribution", "direction": "bearish", "htf_bias": "bearish", "itf_bias": "bearish", "reasoning": "test"},
  "trap_analysis": {"type": "None", "retail_sl_cluster": null, "sweep_target": null, "description": "test"},
  "confluence": {"score": 40, "factors": {}, "conflicts": []},
  "key_levels": [],
  "execution": {
    "decision": "NO_TRADE", "direction": null, "entry_type": null, "entry_price": null,
    "entry_zone": [null, null], "stop_loss": null, "tp1": null, "tp2": null,
    "tp1_close_pct": 50, "rr_ratio": null, "confidence": 40, "invalidation": null,
    "recommended_leverage": null, "no_trade_reason": "Confluence too low", "wait_for": null
  },
  "narrative": "No trade."
}"""


def _trailing_comma_json_text() -> str:
    return """{
  "institutional_bias": {"phase": "Distribution", "direction": "bearish", "htf_bias": "bearish", "itf_bias": "bearish", "reasoning": "test",},
  "trap_analysis": {"type": "None", "retail_sl_cluster": null, "sweep_target": null, "description": "test"},
  "confluence": {"score": 40, "factors": {}, "conflicts": [],},
  "key_levels": [1, 2, ],
  "execution": {
    "decision": "NO_TRADE", "direction": null, "entry_type": null, "entry_price": null,
    "entry_zone": [null, null], "stop_loss": null, "tp1": null, "tp2": null,
    "tp1_close_pct": 50, "rr_ratio": null, "confidence": 40, "invalidation": null,
    "recommended_leverage": null, "no_trade_reason": "Confluence too low", "wait_for": null,
  },
  "narrative": "No trade."
}"""


def _comments_json_text() -> str:
    return """{
  // top-level comment before a field
  "institutional_bias": {"phase": "Distribution", "direction": "bearish", "htf_bias": "bearish", "itf_bias": "bearish", "reasoning": "test"},
  /* block
     comment */
  "trap_analysis": {"type": "None", "retail_sl_cluster": null, "sweep_target": null, "description": "test"},
  "confluence": {"score": 40, "factors": {}, "conflicts": []}, // inline comment after a value
  "key_levels": [],
  "execution": {
    "decision": "NO_TRADE", "direction": null, "entry_type": null, "entry_price": null,
    "entry_zone": [null, null], "stop_loss": null, "tp1": null, "tp2": null,
    "tp1_close_pct": 50, "rr_ratio": null, "confidence": 40, "invalidation": null,
    "recommended_leverage": null, "no_trade_reason": "Confluence too low", "wait_for": null
  },
  "narrative": "No trade."
}"""


def _string_preserving_json_text() -> str:
    # A genuine trailing comma (confluence.conflicts) forces the repair
    # pass to run; the no_trade_reason string itself contains "//", "/*"
    # and ", }" and must survive completely untouched.
    return """{
  "institutional_bias": {"phase": "Distribution", "direction": "bearish", "htf_bias": "bearish", "itf_bias": "bearish", "reasoning": "test"},
  "trap_analysis": {"type": "None", "retail_sl_cluster": null, "sweep_target": null, "description": "test"},
  "confluence": {"score": 40, "factors": {}, "conflicts": [],},
  "key_levels": [],
  "execution": {
    "decision": "NO_TRADE", "direction": null, "entry_type": null, "entry_price": null,
    "entry_zone": [null, null], "stop_loss": null, "tp1": null, "tp2": null,
    "tp1_close_pct": 50, "rr_ratio": null, "confidence": 40, "invalidation": null,
    "recommended_leverage": null,
    "no_trade_reason": "line // not a comment /* also not a comment */ trailing, }",
    "wait_for": null
  },
  "narrative": "No trade."
}"""


def _escaped_quote_json_text() -> str:
    # execution has a genuine trailing comma before its closing } (forces
    # the repair pass); no_trade_reason contains an escaped quote that the
    # string-aware scanner must not mistake for the string's closing quote.
    return r"""{
  "institutional_bias": {"phase": "Distribution", "direction": "bearish", "htf_bias": "bearish", "itf_bias": "bearish", "reasoning": "test"},
  "trap_analysis": {"type": "None", "retail_sl_cluster": null, "sweep_target": null, "description": "test"},
  "confluence": {"score": 40, "factors": {}, "conflicts": []},
  "key_levels": [],
  "execution": {
    "decision": "NO_TRADE", "direction": null, "entry_type": null, "entry_price": null,
    "entry_zone": [null, null], "stop_loss": null, "tp1": null, "tp2": null,
    "tp1_close_pct": 50, "rr_ratio": null, "confidence": 40, "invalidation": null,
    "recommended_leverage": null,
    "no_trade_reason": "she said \"hold\" clearly",
    "wait_for": null,
  },
  "narrative": "No trade."
}"""


# ─────────────────────────────────────────────────────────────────────────────

class TestJsonRepair(unittest.TestCase):
    def test_valid_json_parses_identically_before_and_after(self):
        text = _valid_no_trade_json_text()
        result = _parse_json_response(text)
        self.assertEqual(result, json.loads(text))
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        self.assertEqual(result["execution"]["no_trade_reason"], "Confluence too low")

    def test_trailing_comma_before_brace_and_bracket_is_repaired(self):
        result = _parse_json_response(_trailing_comma_json_text())
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        self.assertEqual(result["key_levels"], [1, 2])
        self.assertEqual(result["confluence"]["score"], 40)

    def test_line_and_block_comments_are_repaired(self):
        result = _parse_json_response(_comments_json_text())
        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        self.assertEqual(result["confluence"]["score"], 40)
        self.assertEqual(result["trap_analysis"]["type"], "None")

    def test_string_containing_comment_and_comma_brace_markers_survives_untouched(self):
        text = _string_preserving_json_text()
        # Sanity: this text really does need repair (strict parse fails).
        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)

        result = _parse_json_response(text)
        self.assertEqual(
            result["execution"]["no_trade_reason"],
            "line // not a comment /* also not a comment */ trailing, }",
        )

    def test_escaped_quote_in_string_with_trailing_comma_elsewhere(self):
        text = _escaped_quote_json_text()
        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)

        result = _parse_json_response(text)
        self.assertEqual(result["execution"]["no_trade_reason"], 'she said "hold" clearly')

    def test_unclosed_brace_raises_original_error_unchanged(self):
        text = '{"institutional_bias": {"phase": "Distribution"'
        with self.assertRaises(json.JSONDecodeError) as strict_ctx:
            json.loads(text)
        expected_message = str(strict_ctx.exception)

        with self.assertRaises(json.JSONDecodeError) as repaired_ctx:
            _parse_json_response(text)
        self.assertEqual(str(repaired_ctx.exception), expected_message)

    def test_repair_helpers_are_no_ops_on_already_valid_json(self):
        from app.services.claude_service import _repair_near_json
        text = _valid_no_trade_json_text()
        repaired, applied = _repair_near_json(text)
        self.assertEqual(repaired, text)
        self.assertEqual(applied, [])


# ─────────────────────────────────────────────────────────────────────────────
# run_analysis end-to-end: repair happens transparently; truncation still
# reports truncation, not a parse error (self-contained stubs, no network).
# ─────────────────────────────────────────────────────────────────────────────

class _TextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Message:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def _minimal_snapshot():
    return {
        "symbol": "BTC/USDT",
        "ticker": {"last": 65000, "high": 66000, "low": 64000, "volume": 1000, "change_pct": 1.5},
        "candles_1d": [
            {"timestamp": "2024-01-01", "open": 63000, "high": 64000, "low": 62000, "close": 63500, "volume": 100},
        ],
        "candles_4h": [], "candles_1h": [],
        "funding_rate": 0.0001, "fear_greed_index": 65,
    }


class TestRunAnalysisWithRepair(unittest.TestCase):
    def test_trailing_comma_response_parses_on_first_attempt_no_retry_needed(self):
        msg = _Message([_TextBlock(_trailing_comma_json_text())])
        with patch("app.services.claude_service._get_client") as mc:
            mc.return_value.messages.create.return_value = msg
            result = run_analysis(_minimal_snapshot())

        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        # repaired on the first attempt -- no retry call needed
        self.assertEqual(mc.return_value.messages.create.call_count, 1)

    def test_max_tokens_truncation_still_reports_truncation_not_parse_error(self):
        # Otherwise-parseable (even repairable) body, but stop_reason says
        # it was cut off -- truncation must still be checked and reported
        # before the repair pass (or strict parse) ever runs.
        msg = _Message([_TextBlock(_trailing_comma_json_text())], stop_reason="max_tokens")
        with patch("app.services.claude_service._get_client") as mc:
            mc.return_value.messages.create.return_value = msg
            result = run_analysis(_minimal_snapshot())

        self.assertEqual(result["execution"]["decision"], "NO_TRADE")
        reason = result["execution"]["no_trade_reason"]
        self.assertIn("truncated at max_tokens", reason)
        self.assertNotIn("Expecting", reason)  # not a json.JSONDecodeError message


if __name__ == "__main__":
    unittest.main()
