"""
Claude AI analysis service — Institutional SMC methodology.

Phase 3 upgrade:
  ✓ Pure JSON schema output — no free-text, no regex, no markdown parsing
  ✓ System prompt enforces JSON-only response
  ✓ Input schema extended: 15m candles, CVD, OI, kill zone, pre-computed SMC levels
  ✓ Strict validation after json.loads() — AnalysisValidationError on contract breach
  ✓ Retry logic: malformed/invalid JSON — 1 retry with correction context
  ✓ Safe NO_TRADE fallback if all retries fail (never raises to caller)
  ✓ 100% backward compatible with executor.py and scheduler.py

Output contract (unchanged shape — same keys scheduler & executor expect):
  result["symbol"]          — str
  result["bias"]            — "bullish" | "bearish" | "neutral"
  result["confidence"]      — int | None  (0–100)
  result["key_levels"]      — list[dict]
  result["trade_idea"]      — str  (narrative)
  result["full_analysis"]   — str  (raw JSON string)
  result["raw_prompt"]      — str
  result["trade_direction"] — "LONG" | "SHORT" | "WAIT" | None
  result["trade_entry"]     — float | None
  result["trade_sl"]        — float | None
  result["trade_tp"]        — float | None  → TP1
  result["execution"]       — dict (consumed by executor.py)
      .decision             — "TRADE" | "NO_TRADE" | "WAIT"
      .direction            — "LONG" | "SHORT" | None
      .entry_type           — "LIMIT" | "MARKET" | None
      .entry_price          — float | None
      .entry_zone           — [float|None, float|None]
      .stop_loss            — float | None
      .tp1                  — float | None
      .tp2                  — float | None
      .tp1_close_pct        — int (default 50)
      .rr_ratio             — float | None
      .confidence           — int (0–100)
      .invalidation         — str | None
      .recommended_leverage — int | None
      .no_trade_reason      — str | None
      .wait_for             — str | None
      .symbol               — str  — injected for executor.py
  result["analysis"]        — dict  (full parsed JSON, all sections)
  result["open_price"], ["high_price"], ["low_price"], ["close_price"]
  result["volume"], ["funding_rate"], ["fear_greed_index"]
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# System prompt — enforces pure JSON output, J.P. Morgan SMC persona
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a Senior Executive Trader at J.P. Morgan Global Markets Division, specialising exclusively in Institutional Order Flow and Smart Money Concepts (SMC / ICT methodology).

ABSOLUTE RULES:
1. Respond with ONLY a single valid JSON object. No preamble, no markdown, no code fences, no explanation outside the JSON. First character must be '{'.
2. Do NOT reference retail indicators (RSI, MACD, Bollinger Bands, EMA crossovers, stochastic, etc.). Use ONLY: Liquidity, Order Block, Fair Value Gap / Imbalance, Market Structure (BOS / CHoCH), Inducement, Optimal Trade Entry (OTE), Kill Zone context.
3. Minimum Risk:Reward for TRADE decision = 1:3. If unachievable — decision = "NO_TRADE".
4. Minimum confidence for TRADE = 85. Below 85 — decision = "NO_TRADE" or "WAIT".
5. Contradictory confluence or unclear market structure — decision = "NO_TRADE".

OUTPUT SCHEMA — return exactly this JSON, all keys present, correct types:
{
  "institutional_bias": {
    "phase": "<Accumulation|Manipulation|Distribution|Reaccumulation|Redistribution>",
    "direction": "<bullish|bearish|neutral>",
    "htf_bias": "<bullish|bearish|neutral>",
    "itf_bias": "<bullish|bearish|neutral>",
    "reasoning": "<max 200 chars — structural reasoning, no retail indicator references>"
  },
  "trap_analysis": {
    "type": "<Bull Trap|Bear Trap|Liquidity Sweep|Inducement|None>",
    "retail_sl_cluster": <float price or null>,
    "sweep_target": <float price or null>,
    "description": "<max 150 chars>"
  },
  "confluence": {
    "score": <integer 0-100>,
    "factors": {
      "<factor_name>": "<bullish|bearish|neutral>"
    },
    "conflicts": ["<conflict string>"]
  },
  "key_levels": [
    {
      "type": "<Order Block Bullish|Order Block Bearish|Fair Value Gap Bullish|Fair Value Gap Bearish|Imbalance|Liquidity Zone|Equal Highs|Equal Lows|Premium Zone|Discount Zone|BOS|CHoCH>",
      "price": <float mid-price or null if zone>,
      "low": <float zone-low or null>,
      "high": <float zone-high or null>,
      "tf": "<1D|4H|1H|15m>",
      "strength": <integer 1-10>
    }
  ],
  "execution": {
    "decision": "<TRADE|NO_TRADE|WAIT>",
    "direction": "<LONG|SHORT|null>",
    "entry_type": "<LIMIT|MARKET|null>",
    "entry_price": <float or null>,
    "entry_zone": [<float low or null>, <float high or null>],
    "stop_loss": <float or null>,
    "tp1": <float or null>,
    "tp2": <float or null>,
    "tp1_close_pct": <integer, default 50>,
    "rr_ratio": <float or null>,
    "confidence": <integer 0-100>,
    "invalidation": "<max 100 chars or null>",
    "recommended_leverage": <integer 1-20 or null>,
    "no_trade_reason": "<string or null — required when decision is NO_TRADE or WAIT>",
    "wait_for": "<string or null — describe trigger to re-evaluate when WAIT>"
  },
  "narrative": "<max 300 chars — institutional summary in Bahasa Indonesia>"
}"""


# ─────────────────────────────────────────────────────────────────────────────
# Market data formatters
# ─────────────────────────────────────────────────────────────────────────────

def _format_candles(candles: list, label: str, limit: int = 10) -> str:
    if not candles:
        return f"{label}: no data"
    rows = []
    for c in candles[-limit:]:
        ts = str(c.get("timestamp", ""))[:16]
        rows.append(
            f"  {ts} | O:{c['open']} H:{c['high']} L:{c['low']}"
            f" C:{c['close']} V:{c.get('volume', 0):.0f}"
        )
    return f"{label} (last {len(rows)} bars):\n" + "\n".join(rows)


def _market_structure(candles: list) -> str:
    """Derive simple HH/HL or LH/LL label from last 3 daily closes."""
    if len(candles) < 3:
        return "Insufficient data"
    highs = [c["high"] for c in candles[-3:]]
    lows  = [c["low"]  for c in candles[-3:]]
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "HH/HL (Uptrend)"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "LH/LL (Downtrend)"
    return "Ranging / Indecisive"


def _weekly_range(candles: list) -> str:
    """High and Low of the last 7 daily candles."""
    week = candles[-7:] if len(candles) >= 7 else candles
    if not week:
        return "N/A"
    wh = max(c["high"] for c in week)
    wl = min(c["low"]  for c in week)
    return f"High:{wh:,.4f} | Low:{wl:,.4f}"


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder — extended input schema (Phase 3 + Phase 2-ready)
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt(snapshot: Dict) -> str:
    """
    Build user message from market snapshot dict.

    Snapshot keys (required):
      symbol, ticker, candles_1d, candles_4h, candles_1h

    Snapshot keys (optional — Phase 2 pre-compute additions):
      candles_15m    — 15m OHLCV candles
      kill_zone      — Current session: "LONDON_OPEN"|"NY_AM"|"NY_PM"|"ASIA"|"DEAD"
      cvd            — Cumulative Volume Delta (float or dict)
      open_interest  — Open Interest in USD (float)
      smc_levels     — Pre-computed OBs, FVGs, BOS/CHoCH from Phase 2 engine
    """
    symbol      = snapshot["symbol"]
    ticker      = snapshot.get("ticker", {})
    candles_1d  = snapshot.get("candles_1d", [])
    candles_4h  = snapshot.get("candles_4h", [])
    candles_1h  = snapshot.get("candles_1h", [])
    candles_15m = snapshot.get("candles_15m", [])
    funding     = snapshot.get("funding_rate")
    fg          = snapshot.get("fear_greed_index")

    # Phase 2 optional fields
    kill_zone  = snapshot.get("kill_zone", "UNKNOWN")
    cvd        = snapshot.get("cvd")
    oi         = snapshot.get("open_interest")
    smc_levels = snapshot.get("smc_levels", {})

    funding_str = f"{funding * 100:.4f}%" if funding is not None else "N/A"
    fg_str      = f"{fg}/100" if fg is not None else "N/A"

    # Build candle sections
    candle_sections = [
        _format_candles(candles_1d,  "1D  (HTF)",  10),
        _format_candles(candles_4h,  "4H  (ITF)",  12),
        _format_candles(candles_1h,  "1H  (LTF)",  12),
    ]
    if candles_15m:
        candle_sections.append(_format_candles(candles_15m, "15m (Execution TF)", 16))
    else:
        candle_sections.append("15m: not provided (requires Phase 2 engine)")

    candle_block = "\n\n".join(candle_sections)

    # Optional market depth data
    depth_lines = []
    if cvd is not None:
        depth_lines.append(f"CVD (Cumulative Volume Delta): {cvd}")
    if oi is not None:
        depth_lines.append(f"Open Interest: {oi:,.2f} USD")
    depth_block = ("\n" + "\n".join(depth_lines)) if depth_lines else ""

    # Optional pre-computed SMC levels from Phase 2 engine
    smc_block = ""
    if smc_levels:
        smc_block = (
            "\n\nPRE-COMPUTED SMC LEVELS (Python Engine — use as confirmation, not replacement):\n"
            + json.dumps(smc_levels, indent=2)
        )

    prompt = f"""ANALISA SMC INSTITUSIONAL — {symbol}

=== MARKET DATA ===
Pair              : {symbol}
Last Price        : {ticker.get('last', 'N/A')}
24h High / Low    : {ticker.get('high', 'N/A')} / {ticker.get('low', 'N/A')}
24h Volume        : {ticker.get('volume', 'N/A')}
24h Change        : {ticker.get('change_pct', 'N/A')}%
Funding Rate      : {funding_str}
Fear & Greed      : {fg_str}
Kill Zone         : {kill_zone}
Market Structure  : {_market_structure(candles_1d)}
Weekly Range (1D) : {_weekly_range(candles_1d)}{depth_block}

=== CANDLE DATA ===
{candle_block}{smc_block}

=== INSTRUKSI ANALISA ===
1. INSTITUTIONAL BIAS: Tentukan fase pasar (Akumulasi/Manipulasi/Distribusi/Reakumulasi/Redistribusi). Jelaskan alasan struktural.
2. TRAP ANALYSIS: Identifikasi Bull/Bear Trap atau Liquidity Sweep jika ada. Tunjukkan cluster SL ritel dan target sweep.
3. CONFLUENCE CHECK: Hitung score 0-100. Gabungkan: Market Structure + Liquidity + OB/FVG + Funding Rate + Kill Zone. Jika data bertentangan, tambahkan ke conflicts[].
4. KEY SMC LEVELS: Daftarkan OB, FVG, BOS/CHoCH, Liquidity Zones paling relevan.
5. EXECUTION PLAN: TRADE hanya jika probabilitas institusional > 85% DAN R:R >= 1:3. Jika tidak — NO_TRADE (alasan wajib) atau WAIT (trigger wajib).

Return HANYA JSON object sesuai schema di system prompt. Tidak ada teks di luar JSON."""

    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# JSON validation — schema contract enforcement
# ─────────────────────────────────────────────────────────────────────────────

class AnalysisValidationError(Exception):
    """Raised when Claude's JSON response violates the output contract."""
    pass


def _validate_analysis(data: Dict) -> None:
    """
    Validate Claude's parsed JSON against the output contract.
    Raises AnalysisValidationError with a descriptive message on any violation.
    """
    # Top-level keys
    required_keys = {
        "institutional_bias", "trap_analysis", "confluence",
        "key_levels", "execution", "narrative"
    }
    missing = required_keys - set(data.keys())
    if missing:
        raise AnalysisValidationError(f"Missing top-level keys: {missing}")

    # institutional_bias.direction
    ib = data.get("institutional_bias", {})
    if ib.get("direction") not in ("bullish", "bearish", "neutral"):
        raise AnalysisValidationError(
            f"institutional_bias.direction must be bullish|bearish|neutral, "
            f"got: {ib.get('direction')!r}"
        )

    # confluence.score
    score = data.get("confluence", {}).get("score")
    if score is None or not isinstance(score, (int, float)) or not (0 <= int(score) <= 100):
        raise AnalysisValidationError(
            f"confluence.score must be integer 0-100, got: {score!r}"
        )

    # key_levels must be a list
    if not isinstance(data.get("key_levels"), list):
        raise AnalysisValidationError("key_levels must be a JSON array")

    # execution block
    exec_d   = data.get("execution", {})
    decision = exec_d.get("decision")

    if decision not in ("TRADE", "NO_TRADE", "WAIT"):
        raise AnalysisValidationError(
            f"execution.decision must be TRADE|NO_TRADE|WAIT, got: {decision!r}"
        )

    confidence = exec_d.get("confidence")
    if confidence is None or not isinstance(confidence, (int, float)):
        raise AnalysisValidationError(
            f"execution.confidence must be a number, got: {confidence!r}"
        )

    if decision == "TRADE":
        # All critical execution fields must be non-null
        for field in ("direction", "entry_price", "stop_loss", "tp1", "tp2"):
            if exec_d.get(field) is None:
                raise AnalysisValidationError(
                    f"execution.{field} must not be null when decision=TRADE"
                )
        # Direction must be LONG or SHORT
        if exec_d.get("direction") not in ("LONG", "SHORT"):
            raise AnalysisValidationError(
                f"execution.direction must be LONG|SHORT for TRADE, "
                f"got: {exec_d.get('direction')!r}"
            )
        # R:R sanity floor (strict 3.0 enforced by system prompt, 1.0 here as hard floor)
        rr = exec_d.get("rr_ratio")
        if rr is None or float(rr) < 1.0:
            raise AnalysisValidationError(
                f"execution.rr_ratio must be >= 1.0 for TRADE, got: {rr!r}"
            )
        # Confidence sanity floor
        if int(confidence) < 50:
            raise AnalysisValidationError(
                f"Suspiciously low confidence for TRADE: {confidence} "
                f"(system prompt requires >= 85 — possible hallucination)"
            )
    else:
        # NO_TRADE and WAIT must include a reason
        if not exec_d.get("no_trade_reason"):
            raise AnalysisValidationError(
                f"execution.no_trade_reason must not be null/empty "
                f"when decision={decision}"
            )


def _parse_json_response(raw_text: str) -> Dict:
    """
    Parse Claude's response text as JSON and validate schema.

    Defensive behaviour:
      - Strips accidental markdown fences (```json ... ```) if present.
      - Finds first '{' if there's unexpected leading text.
      - Raises json.JSONDecodeError if not parseable.
      - Raises AnalysisValidationError if schema contract is violated.
    """
    text = raw_text.strip()

    # Defensive: strip markdown fences if accidentally included
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1  # skip opening fence line
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == "```":
                end = i
                break
        text = "\n".join(lines[start:end]).strip()

    # Defensive: find first '{' if there's any leading prose
    if text and text[0] != "{":
        brace_pos = text.find("{")
        if brace_pos != -1:
            text = text[brace_pos:]

    data = json.loads(text)   # raises json.JSONDecodeError on failure
    _validate_analysis(data)  # raises AnalysisValidationError on schema violation
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Safe NO_TRADE fallback
# ─────────────────────────────────────────────────────────────────────────────

def _no_trade_fallback(symbol: str, reason: str) -> Dict:
    """Return a structurally valid NO_TRADE analysis dict when all parsing fails."""
    return {
        "institutional_bias": {
            "phase":     "Unknown",
            "direction": "neutral",
            "htf_bias":  "neutral",
            "itf_bias":  "neutral",
            "reasoning": reason[:200],
        },
        "trap_analysis": {
            "type":              "None",
            "retail_sl_cluster": None,
            "sweep_target":      None,
            "description":       reason[:150],
        },
        "confluence": {
            "score":     0,
            "factors":   {},
            "conflicts": [reason],
        },
        "key_levels": [],
        "execution": {
            "decision":             "NO_TRADE",
            "direction":            None,
            "entry_type":           None,
            "entry_price":          None,
            "entry_zone":           [None, None],
            "stop_loss":            None,
            "tp1":                  None,
            "tp2":                  None,
            "tp1_close_pct":        50,
            "rr_ratio":             None,
            "confidence":           0,
            "invalidation":         None,
            "recommended_leverage": None,
            "no_trade_reason":      reason[:300],
            "wait_for":             None,
        },
        "narrative": f"NO TRADE — {reason[:250]}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Backward compatibility mapper
# ─────────────────────────────────────────────────────────────────────────────

def _to_legacy_result(
    analysis: Dict,
    snapshot: Dict,
    prompt: str,
    raw_json: str,
) -> Dict:
    """
    Map Phase 3 JSON schema → legacy result dict shape.

    Ensures 100% backward compatibility with:
      - scheduler.py  (DailyAnalysis DB record construction, lines 80-99)
      - scheduler.py  (execute_signal call, line 113)
      - executor.py   (execute_signal argument parsing, lines 426-441)
    """
    ib     = analysis["institutional_bias"]
    exec_d = analysis["execution"]

    # Inject symbol into execution dict so executor.py can read it
    exec_d["symbol"] = snapshot["symbol"]

    # Legacy trade_direction — "WAIT" when not trading
    if exec_d.get("decision") == "TRADE":
        trade_direction = exec_d.get("direction")   # "LONG" | "SHORT"
    else:
        trade_direction = "WAIT"

    candles_1d  = snapshot.get("candles_1d", [])
    last_candle = candles_1d[-1] if candles_1d else {}

    return {
        # ── Core ──────────────────────────────────────────────────────────────
        "symbol":           snapshot["symbol"],

        # ── Legacy fields (DailyAnalysis DB model keys) ───────────────────────
        "bias":             ib.get("direction", "neutral"),
        "confidence":       exec_d.get("confidence"),
        "key_levels":       analysis.get("key_levels", []),
        "trade_idea":       analysis.get("narrative", ""),
        "full_analysis":    raw_json,         # JSON string (was free-text)
        "raw_prompt":       prompt,
        "trade_direction":  trade_direction,
        "trade_entry":      exec_d.get("entry_price"),
        "trade_sl":         exec_d.get("stop_loss"),
        "trade_tp":         exec_d.get("tp1"),  # TP1 = legacy trade_tp

        # ── OHLCV from last daily candle ──────────────────────────────────────
        "open_price":       last_candle.get("open"),
        "high_price":       last_candle.get("high"),
        "low_price":        last_candle.get("low"),
        "close_price":      last_candle.get("close"),
        "volume":           last_candle.get("volume"),
        "funding_rate":     snapshot.get("funding_rate"),
        "fear_greed_index": snapshot.get("fear_greed_index"),

        # ── New fields ────────────────────────────────────────────────────────
        # Used by executor.py execute_signal() directly
        "execution":        exec_d,
        # Full parsed analysis — for Phase 4/5 UI and future consumers
        "analysis":         analysis,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Claude API client
# ─────────────────────────────────────────────────────────────────────────────

def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_analysis(snapshot: Dict, max_retries: int = 1) -> Dict:
    """
    Main entry: build prompt → call Claude (JSON mode) → validate → return result.

    Retry policy:
      Attempt 0 : normal call.
      Attempt 1+ : retry with correction context (Claude sees its prior bad response).
      All failed  : return safe NO_TRADE fallback — never raises to caller.

    Args:
        snapshot:    Market data dict from binance_service.fetch_market_snapshot().
                     Optional keys: candles_15m, kill_zone, cvd, open_interest, smc_levels
        max_retries: Number of retries on JSON parse/validation failure (default 1).

    Returns:
        Backward-compatible result dict — see module docstring for full key list.
    """
    prompt = build_prompt(snapshot)
    client = _get_client()
    symbol = snapshot.get("symbol", "UNKNOWN")

    attempt:    int           = 0
    last_error: Optional[str] = None
    raw_text:   str           = ""

    while attempt <= max_retries:
        try:
            if attempt == 0:
                messages = [{"role": "user", "content": prompt}]
            else:
                # Retry with correction context — Claude sees its prior invalid response
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": raw_text},
                    {
                        "role": "user",
                        "content": (
                            f"Respons sebelumnya tidak valid: {last_error}. "
                            "Kembalikan HANYA JSON object yang valid sesuai schema di system prompt. "
                            "Tidak boleh ada teks, markdown, atau penjelasan di luar JSON. "
                            "Karakter pertama HARUS '{'."
                        ),
                    },
                ]

            message = client.messages.create(
                model=settings.claude_model,
                max_tokens=4096,
                system=_SYSTEM_PROMPT,
                messages=messages,
            )

            raw_text = message.content[0].text
            analysis = _parse_json_response(raw_text)

            exec_d = analysis["execution"]
            logger.info(
                "[Claude] %s — decision=%s confidence=%s bias=%s rr=%.1f",
                symbol,
                exec_d.get("decision", "?"),
                exec_d.get("confidence", "?"),
                analysis["institutional_bias"].get("direction", "?"),
                float(exec_d.get("rr_ratio") or 0),
            )
            return _to_legacy_result(analysis, snapshot, prompt, raw_text)

        except (json.JSONDecodeError, AnalysisValidationError) as e:
            last_error = str(e)
            attempt += 1
            logger.warning(
                "[Claude] %s JSON parse/validation failed (attempt %d/%d): %s",
                symbol, attempt, max_retries + 1, e
            )

        except anthropic.APIError as e:
            logger.error("[Claude] %s API error: %s", symbol, e, exc_info=True)
            last_error = f"Anthropic API error: {e}"
            break

        except Exception as e:
            logger.error("[Claude] %s unexpected error: %s", symbol, e, exc_info=True)
            last_error = str(e)
            break

    # ── All attempts failed — safe NO_TRADE fallback ──────────────────────────
    reason = f"Analysis unavailable after {attempt} attempt(s): {last_error}"
    logger.error("[Claude] %s falling back to NO_TRADE: %s", symbol, reason)

    fallback = _no_trade_fallback(symbol, reason)
    return _to_legacy_result(fallback, snapshot, prompt, json.dumps(fallback))
