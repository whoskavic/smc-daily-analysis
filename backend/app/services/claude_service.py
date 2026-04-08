"""
Claude AI analysis service.
Builds a structured SMC (Smart Money Concepts) prompt from market data
and returns parsed analysis output.
"""
import json
import anthropic
from typing import Dict, Optional
from app.config import settings


def _get_client():
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _format_candles_table(candles: list, label: str) -> str:
    if not candles:
        return f"{label}: no data\n"
    lines = [f"\n### {label} (last {len(candles)} bars)"]
    lines.append("timestamp | open | high | low | close | volume")
    lines.append("--- | --- | --- | --- | --- | ---")
    for c in candles[-10:]:   # send only last 10 to keep prompt lean
        lines.append(
            f"{c['timestamp'][:16]} | {c['open']} | {c['high']} | {c['low']} | {c['close']} | {c['volume']:.0f}"
        )
    return "\n".join(lines)


def build_prompt(snapshot: Dict) -> str:
    symbol = snapshot["symbol"]
    ticker = snapshot["ticker"]
    funding = snapshot.get("funding_rate")
    fg = snapshot.get("fear_greed_index")

    funding_str = f"{funding*100:.4f}%" if funding is not None else "N/A"
    fg_str = str(fg) if fg is not None else "N/A"

    candle_tables = (
        _format_candles_table(snapshot.get("candles_1d", []), "Daily (1D)")
        + _format_candles_table(snapshot.get("candles_4h", []), "4-Hour (4H)")
        + _format_candles_table(snapshot.get("candles_1h", []), "1-Hour (1H)")
    )

    prompt = f"""You are an expert crypto trader specializing in Smart Money Concepts (SMC) and ICT methodology.
Analyze the following market data for {symbol} and provide your daily trading analysis.

## Current Market Data
- Symbol: {symbol}
- Last Price: {ticker.get('last')}
- 24h High: {ticker.get('high')}  |  24h Low: {ticker.get('low')}
- 24h Volume: {ticker.get('volume')}
- 24h Change: {ticker.get('change_pct')}%
- Perpetual Funding Rate: {funding_str}
- Fear & Greed Index: {fg_str} / 100
{candle_tables}

## Your Analysis Task
Based on the data above, provide a structured SMC analysis with the following sections:

### 1. MARKET BIAS
State: BULLISH / BEARISH / NEUTRAL
Confidence: X/100
Reasoning: (2-3 sentences explaining the bias based on structure)

### 2. MARKET STRUCTURE
- Current trend on each timeframe (1D, 4H, 1H)
- Recent Break of Structure (BOS) or Change of Character (ChoCH) if any
- Higher Highs / Higher Lows or Lower Highs / Lower Lows pattern

### 3. KEY SMC LEVELS
List the most important levels in JSON format like this (include the JSON block):
```json
{{
  "key_levels": [
    {{"type": "Order Block Bullish", "price": 0.00, "timeframe": "4H", "description": "..."}},
    {{"type": "Fair Value Gap", "price": 0.00, "timeframe": "1H", "description": "..."}},
    {{"type": "Liquidity Zone", "price": 0.00, "timeframe": "1D", "description": "..."}}
  ]
}}
```
Types can be: Order Block Bullish, Order Block Bearish, Fair Value Gap, Imbalance, Liquidity Zone, Equal Highs, Equal Lows, Premium Zone, Discount Zone, Support, Resistance

### 4. TRADE IDEA
- Direction: LONG / SHORT / WAIT
- Entry Zone: price range
- Stop Loss: price level (with reasoning)
- Take Profit 1: price level
- Take Profit 2: price level (optional)
- Risk/Reward: ratio
- Entry Trigger: what price action confirmation to look for before entering

### 5. RISK NOTES
Any macro or market-specific risks to be aware of today (funding rate extremes, upcoming events, etc.)

Be concise, specific, and base everything on the data provided. Do not speculate beyond what the data shows."""

    return prompt


def parse_analysis(raw_text: str, snapshot: Dict) -> Dict:
    """Extract structured fields from Claude's response."""
    bias = "neutral"
    confidence = 50
    key_levels = []
    trade_idea = ""

    text_lower = raw_text.lower()

    # Bias
    if "bullish" in text_lower:
        bias = "bullish"
    elif "bearish" in text_lower:
        bias = "bearish"

    # Confidence
    import re
    conf_match = re.search(r"confidence[:\s]+(\d+)/100", raw_text, re.IGNORECASE)
    if conf_match:
        confidence = int(conf_match.group(1))

    # Key levels JSON block
    json_match = re.search(r"```json\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            key_levels = parsed.get("key_levels", [])
        except json.JSONDecodeError:
            pass

    # Trade idea section
    trade_match = re.search(r"### 4\. TRADE IDEA(.*?)(?=### 5\.|$)", raw_text, re.DOTALL)
    if trade_match:
        trade_idea = trade_match.group(1).strip()

    # Extract structured trade plan prices from table format
    def table_price(keyword):
        pattern = re.compile(
            rf"\|[^|]*{keyword}[^|]*\|[^|*]*\**([0-9][0-9,]*(?:\.[0-9]*)?)",
            re.IGNORECASE
        )
        m = pattern.search(raw_text)
        return float(m.group(1).replace(",", "")) if m else None

    trade_direction = None
    dir_match = re.search(r"direction[:\s*|]+\**\s*(LONG|SHORT|WAIT)", raw_text, re.IGNORECASE)
    if dir_match:
        trade_direction = dir_match.group(1).upper()

    trade_entry = table_price("Entry Zone") or table_price("Entry")
    trade_sl    = table_price("Stop Loss")
    trade_tp    = table_price("Take Profit 1") or table_price("Take Profit")

    ticker = snapshot.get("ticker", {})
    candles_1d = snapshot.get("candles_1d", [])
    last_candle = candles_1d[-1] if candles_1d else {}

    return {
        "symbol": snapshot["symbol"],
        "bias": bias,
        "confidence": confidence,
        "key_levels": key_levels,
        "trade_idea": trade_idea,
        "trade_direction": trade_direction,
        "trade_entry": trade_entry,
        "trade_sl": trade_sl,
        "trade_tp": trade_tp,
        "full_analysis": raw_text,
        "open_price": last_candle.get("open"),
        "high_price": last_candle.get("high"),
        "low_price": last_candle.get("low"),
        "close_price": last_candle.get("close"),
        "volume": last_candle.get("volume"),
        "funding_rate": snapshot.get("funding_rate"),
        "fear_greed_index": snapshot.get("fear_greed_index"),
    }


def run_analysis(snapshot: Dict) -> Dict:
    """Main entry: build prompt → call Claude → parse → return structured result."""
    prompt = build_prompt(snapshot)

    message = _get_client().messages.create(
        model=settings.claude_model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = message.content[0].text
    result = parse_analysis(raw_text, snapshot)
    result["raw_prompt"] = prompt
    return result
