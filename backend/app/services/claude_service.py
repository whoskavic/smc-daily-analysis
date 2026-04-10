"""
Claude AI analysis service — Institutional SMC methodology.
Prompt language: Bahasa Indonesia (J.P. Morgan Senior Trader persona).
"""
import json
import re
import anthropic
from typing import Dict
from app.config import settings


def _get_client():
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _format_candles_table(candles: list, label: str) -> str:
    if not candles:
        return f"{label}: no data\n"
    lines = [f"\n### {label} (last {min(len(candles), 10)} bars)"]
    lines.append("timestamp | open | high | low | close | volume")
    lines.append("--- | --- | --- | --- | --- | ---")
    for c in candles[-10:]:
        lines.append(
            f"{c['timestamp'][:16]} | {c['open']} | {c['high']} | {c['low']} | {c['close']} | {c['volume']:.0f}"
        )
    return "\n".join(lines)


def _market_structure(candles: list) -> str:
    """Derive simple HH/HL or LH/LL label from last 3 daily closes."""
    if len(candles) < 3:
        return "Insufficient data"
    closes = [c["close"] for c in candles[-3:]]
    highs  = [c["high"]  for c in candles[-3:]]
    lows   = [c["low"]   for c in candles[-3:]]
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
    return f"Weekly High: {wh:,.2f} | Weekly Low: {wl:,.2f}"


def build_prompt(snapshot: Dict) -> str:
    symbol     = snapshot["symbol"]
    ticker     = snapshot["ticker"]
    candles_1d = snapshot.get("candles_1d", [])
    candles_4h = snapshot.get("candles_4h", [])
    candles_1h = snapshot.get("candles_1h", [])
    funding    = snapshot.get("funding_rate")
    fg         = snapshot.get("fear_greed_index")

    funding_str = f"{funding*100:.4f}%" if funding is not None else "N/A"
    fg_str      = f"{fg}/100" if fg is not None else "N/A"

    mkt_structure = _market_structure(candles_1d)
    weekly_range  = _weekly_range(candles_1d)

    candle_tables = (
        _format_candles_table(candles_1d, "Daily 1D")
        + _format_candles_table(candles_4h, "4-Hour 4H")
        + _format_candles_table(candles_1h, "1-Hour 1H")
    )

    prompt = f"""🧠 ROLE: Bertindaklah sebagai Senior Executive Trader & Market Strategist di J.P. Morgan Global Markets Division dengan spesialisasi Institutional Order Flow & Smart Money Concepts (SMC).

🎯 OBJECTIVE: Melakukan analisa probabilitas tinggi untuk menentukan arah pergerakan harga selanjutnya berdasarkan perspektif Market Maker. Fokus pada profitabilitas institusional dan proteksi modal.

🚫 RULES:
* Abaikan indikator ritel standar dan sinyal umum.
* Gunakan pendekatan Liquidity, Order Block, Imbalance, dan Market Structure.
* Jika setup lemah atau konfluensi tidak solid → nyatakan dengan tegas: NO TRADE.

📊 DATA PASAR:
Pair              : {symbol}
Timeframe Utama   : 1D (konfirmasi 4H & 1H)
Harga Terakhir    : {ticker.get('last')}
24h High / Low    : {ticker.get('high')} / {ticker.get('low')}
24h Volume        : {ticker.get('volume')}
24h Change        : {ticker.get('change_pct')}%
Struktur Market   : {mkt_structure}
Range Penting     : {weekly_range}
Funding Rate      : {funding_str}
Fear & Greed      : {fg_str}
{candle_tables}

🧩 ANALISA DENGAN STRUKTUR WAJIB:

1️⃣ INSTITUTIONAL BIAS
Tentukan apakah pasar sedang: Akumulasi / Manipulasi / Distribusi
Jelaskan alasannya secara struktural (bukan asumsi).

2️⃣ THE TRAP (JEBAKAN RETAIL)
Identifikasi potensi Bull Trap atau Bear Trap.
Tunjukkan di mana stop loss ritel terkumpul dan mengapa harga kemungkinan disapu.

3️⃣ CONFLUENCE CHECK
Gabungkan: Struktur harga + Liquidity + Order Block/Imbalance + Funding/Sentiment.
Jika data saling bertentangan → TULIS: NO TRADE (dengan alasan).

4️⃣ KEY SMC LEVELS
Sertakan level kunci dalam format JSON berikut (wajib ada):
```json
{{
  "key_levels": [
    {{"type": "Order Block Bullish", "price": 0.00, "timeframe": "4H", "description": "..."}},
    {{"type": "Liquidity Zone", "price": 0.00, "timeframe": "1D", "description": "..."}}
  ]
}}
```
Types: Order Block Bullish, Order Block Bearish, Fair Value Gap, Imbalance, Liquidity Zone, Equal Highs, Equal Lows, Premium Zone, Discount Zone, Support, Resistance

5️⃣ EXECUTION PLAN (HANYA JIKA PROBABILITAS > 85%)
Jika tidak ada edge institusional yang solid, tulis NO TRADE dan jelaskan alasannya.
Jika ada edge, gunakan format TEPAT berikut:

Direction  : LONG / SHORT
Entry Point: [harga]
Stop Loss  : [harga]
TP1        : [harga]
TP2        : [harga]
R:R        : 1:[rasio]

🛑 Jika kondisi belum memberikan edge institusional → larang entry: NO TRADE / WAIT."""

    return prompt


def parse_analysis(raw_text: str, snapshot: Dict) -> Dict:
    """Extract structured fields from Claude's institutional SMC response."""
    text_lower = raw_text.lower()

    # ── Bias ──────────────────────────────────────────────────────────────────
    # Priority 1: explicit BIAS or BIAS UTAMA line in the summary box
    # Matches: "BIAS UTAMA : BEARISH", "│  BIAS      : BEARISH", "**BIAS**: BULLISH"
    bias_match = re.search(r"\bBIAS(?:\s+UTAMA)?\**\s*[:\|]\s*\**\s*(BULLISH|BEARISH|NEUTRAL)", raw_text, re.IGNORECASE)
    if bias_match:
        bias = bias_match.group(1).lower()
    else:
        # Priority 2: Verdict line in institutional bias section
        verdict_match = re.search(r"Verdict[:\s*]+.*(DISTRIBUSI|AKUMULASI|MANIPULASI)", raw_text, re.IGNORECASE)
        if verdict_match:
            v = verdict_match.group(1).upper()
            bias = "bearish" if v == "DISTRIBUSI" else ("bullish" if v == "AKUMULASI" else "neutral")
        else:
            # Fallback: broad keyword scan (last resort)
            if "distribusi" in text_lower:
                bias = "bearish"
            elif "akumulasi" in text_lower:
                bias = "bullish"
            elif "bearish" in text_lower:
                bias = "bearish"
            elif "bullish" in text_lower:
                bias = "bullish"
            else:
                bias = "neutral"

    # ── Confidence ────────────────────────────────────────────────────────────
    # Returns None if not found — no arbitrary default
    # Handles: "PROBABILITAS: ~87%", "PROBABILITAS INSTITUSIONAL: ~87%", "CONFIDENCE: 87%"
    confidence = None
    conf_match = re.search(r"probabilitas[^%\n]*?(\d+)%", raw_text, re.IGNORECASE)
    if conf_match:
        confidence = int(conf_match.group(1))
    else:
        conf_match = re.search(r"confidence[:\s]+(\d+)[%/]", raw_text, re.IGNORECASE)
        if conf_match:
            confidence = int(conf_match.group(1))

    # ── Key levels JSON block ─────────────────────────────────────────────────
    # Use a greedy capture between ```json ... ``` to get the full outer object
    key_levels = []
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", raw_text)
    if json_match:
        try:
            key_levels = json.loads(json_match.group(1)).get("key_levels", [])
        except json.JSONDecodeError:
            pass

    # ── Trade idea section ────────────────────────────────────────────────────
    trade_idea = ""
    idea_match = re.search(r"5️⃣ EXECUTION PLAN(.*?)$", raw_text, re.DOTALL)
    if idea_match:
        trade_idea = idea_match.group(1).strip()
    else:
        idea_match = re.search(r"EXECUTION PLAN(.*?)$", raw_text, re.DOTALL)
        if idea_match:
            trade_idea = idea_match.group(1).strip()

    # ── Direction ─────────────────────────────────────────────────────────────
    # Check KEPUTUSAN / final decision line first — this is the authoritative signal
    # "NO TRADE" in the KEPUTUSAN block locks the decision; conditional plan is ignored.
    no_trade = bool(re.search(r"KEPUTUSAN\s*[:\|].*?(NO TRADE|WAIT|STAY OUT)", raw_text, re.IGNORECASE))
    if not no_trade:
        # Also catch standalone NO TRADE declarations outside table
        no_trade = bool(re.search(r"⛔\s*NO TRADE|^\s*NO TRADE\b", raw_text, re.IGNORECASE | re.MULTILINE))

    if no_trade:
        trade_direction = "WAIT"
    else:
        # Only extract direction if there is an actual execution signal
        # No ^ anchor — Claude may prefix lines with ║ (box drawing char)
        dir_match = re.search(r"Direction\s*:\s*(LONG|SHORT)", raw_text, re.IGNORECASE)
        trade_direction = dir_match.group(1).upper() if dir_match else None

    # ── Price extractor ───────────────────────────────────────────────────────
    def extract_price(pattern):
        m = re.search(pattern, raw_text, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    # Only extract prices when there is an actual trade signal
    if trade_direction in ("LONG", "SHORT"):
        trade_entry = extract_price(r"Entry Point\s*:\s*\**([0-9][0-9,]*(?:\.[0-9]*)?)")
        trade_sl    = extract_price(r"Stop Loss\s*:\s*\**([0-9][0-9,]*(?:\.[0-9]*)?)")
        trade_tp    = (
            extract_price(r"TP1\s*:\s*\**([0-9][0-9,]*(?:\.[0-9]*)?)")
            or extract_price(r"Take Profit\s*1?\s*:\s*\**([0-9][0-9,]*(?:\.[0-9]*)?)")
        )
    else:
        trade_entry = trade_sl = trade_tp = None

    candles_1d  = snapshot.get("candles_1d", [])
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
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = message.content[0].text
    result   = parse_analysis(raw_text, snapshot)
    result["raw_prompt"] = prompt
    return result
