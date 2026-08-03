"""
SMC Pre-Computation Engine — Phase 2.

Pure, deterministic functions over OHLCV candle lists: same input always
produces the same output, no I/O, no external calls. This module computes
institutional structure (Order Blocks, Fair Value Gaps, BOS/CHoCH, liquidity
zones, premium/discount) so claude_service.py doesn't have to derive it from
raw candles on every call.

Candle dict shape (matches binance_service.fetch_ohlcv output):
    {timestamp, open, high, low, close, volume}

Output key_levels items match the Phase 3 schema:
    {type, price, low, high, tf, strength}
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

Candle = Dict
Level = Dict

_TF_WEIGHTS = {"1D": 0.4, "4H": 0.3, "1H": 0.2, "15m": 0.1}
_TF_PRIORITY = ["1D", "4H", "1H", "15m"]

_STRUCTURAL_TYPES = {
    "Order Block Bullish", "Order Block Bearish",
    "Fair Value Gap Bullish", "Fair Value Gap Bearish",
    "BOS", "CHoCH",
}


# ─────────────────────────────────────────────────────────────────────────────
# Swing detection (fractal pivots)
# ─────────────────────────────────────────────────────────────────────────────

def detect_swings(candles: List[Candle], lookback: int = 2) -> List[Dict]:
    """Fractal swing highs/lows: strict local extreme over a +/- lookback window."""
    n = len(candles)
    swings: List[Dict] = []
    if n < (2 * lookback + 1):
        return swings

    highs = np.array([c["high"] for c in candles], dtype=float)
    lows = np.array([c["low"] for c in candles], dtype=float)

    for i in range(lookback, n - lookback):
        window_highs = highs[i - lookback:i + lookback + 1]
        if highs[i] == window_highs.max() and np.count_nonzero(window_highs == highs[i]) == 1:
            swings.append({
                "index": i, "type": "high", "price": float(highs[i]),
                "timestamp": candles[i].get("timestamp"),
            })
        window_lows = lows[i - lookback:i + lookback + 1]
        if lows[i] == window_lows.min() and np.count_nonzero(window_lows == lows[i]) == 1:
            swings.append({
                "index": i, "type": "low", "price": float(lows[i]),
                "timestamp": candles[i].get("timestamp"),
            })

    swings.sort(key=lambda s: s["index"])
    return swings


def _zigzag(swings: List[Dict]) -> List[Dict]:
    """Collapse consecutive same-type swings to the more extreme one, giving
    a clean alternating high/low/high/low sequence for structure tracking."""
    if not swings:
        return []
    zz = [dict(swings[0])]
    for s in swings[1:]:
        last = zz[-1]
        if s["type"] == last["type"]:
            if s["type"] == "high" and s["price"] >= last["price"]:
                zz[-1] = dict(s)
            elif s["type"] == "low" and s["price"] <= last["price"]:
                zz[-1] = dict(s)
            # else: less extreme duplicate, discard
        else:
            zz.append(dict(s))
    return zz


# ─────────────────────────────────────────────────────────────────────────────
# BOS / CHoCH
# ─────────────────────────────────────────────────────────────────────────────

def _structure_strength(candles: List[Candle], from_idx: int, to_idx: int) -> int:
    """Scale the % price move driving a structure break into a 1-10 strength."""
    lo, hi = min(from_idx, to_idx), max(from_idx, to_idx)
    segment = candles[lo:hi + 1]
    if len(segment) < 2:
        return 1
    start_price = segment[0]["close"]
    end_price = segment[-1]["close"]
    if not start_price:
        return 1
    pct_move = abs(end_price - start_price) / abs(start_price) * 100
    return int(round(min(10, max(1, pct_move * 2))))


def detect_bos_choch(
    candles: List[Candle],
    swings: Optional[List[Dict]] = None,
    tf: str = "1H",
) -> List[Level]:
    """
    Walk candles chronologically, tracking the most recent unbroken swing
    high/low. A close beyond that level is a structure break:
      - same direction as the established trend  → BOS (continuation)
      - opposite the established trend (or no trend yet) → CHoCH
    """
    if swings is None:
        swings = detect_swings(candles)
    zz = _zigzag(swings)
    if len(zz) < 1:
        return []

    events: List[Level] = []
    trend: Optional[str] = None
    active_high: Optional[Dict] = None
    active_low: Optional[Dict] = None
    pivot_idx = 0

    for i, candle in enumerate(candles):
        while pivot_idx < len(zz) and zz[pivot_idx]["index"] <= i:
            p = zz[pivot_idx]
            if p["type"] == "high":
                active_high = p
            else:
                active_low = p
            pivot_idx += 1

        close = candle["close"]

        if active_high and not active_high.get("_broken") and close > active_high["price"]:
            direction = "bullish"
            event_type = "BOS" if trend in (None, "bullish") else "CHoCH"
            events.append({
                "type": event_type,
                "price": active_high["price"],
                "low": None,
                "high": None,
                "tf": tf,
                "strength": _structure_strength(candles, active_high["index"], i),
                "direction": direction,
                "index": i,
            })
            active_high["_broken"] = True
            trend = "bullish"

        if active_low and not active_low.get("_broken") and close < active_low["price"]:
            direction = "bearish"
            event_type = "BOS" if trend in (None, "bearish") else "CHoCH"
            events.append({
                "type": event_type,
                "price": active_low["price"],
                "low": None,
                "high": None,
                "tf": tf,
                "strength": _structure_strength(candles, active_low["index"], i),
                "direction": direction,
                "index": i,
            })
            active_low["_broken"] = True
            trend = "bearish"

    return events


# ─────────────────────────────────────────────────────────────────────────────
# Order Blocks (derived from BOS/CHoCH structure breaks)
# ─────────────────────────────────────────────────────────────────────────────

def detect_order_blocks(
    candles: List[Candle],
    bos_events: Optional[List[Level]] = None,
    tf: str = "1H",
) -> List[Level]:
    """
    Bullish OB = last bearish (down-close) candle before a bullish break.
    Bearish OB = last bullish (up-close) candle before a bearish break.
    """
    if bos_events is None:
        bos_events = detect_bos_choch(candles, tf=tf)

    obs: List[Level] = []
    for event in bos_events:
        break_idx = event["index"]
        direction = event["direction"]
        ob_idx = None
        if direction == "bullish":
            for j in range(break_idx, -1, -1):
                if candles[j]["close"] < candles[j]["open"]:
                    ob_idx = j
                    break
        else:
            for j in range(break_idx, -1, -1):
                if candles[j]["close"] > candles[j]["open"]:
                    ob_idx = j
                    break
        if ob_idx is None:
            continue

        c = candles[ob_idx]
        obs.append({
            "type": "Order Block Bullish" if direction == "bullish" else "Order Block Bearish",
            "price": round((c["high"] + c["low"]) / 2, 8),
            "low": c["low"],
            "high": c["high"],
            "tf": tf,
            "strength": event["strength"],
        })
    return obs


# ─────────────────────────────────────────────────────────────────────────────
# Fair Value Gaps / Imbalance
# ─────────────────────────────────────────────────────────────────────────────

def _gap_strength(candles: List[Candle], gap_low: float, gap_high: float) -> int:
    ranges = [c["high"] - c["low"] for c in candles]
    avg_range = (sum(ranges) / len(ranges)) if ranges else 0
    avg_range = avg_range or 1e-9
    ratio = (gap_high - gap_low) / avg_range
    return int(round(min(10, max(1, ratio * 10))))


def detect_fair_value_gaps(candles: List[Candle], tf: str = "1H") -> List[Level]:
    """3-candle imbalance: gap between candle[i-1].high/low and candle[i+1].low/high."""
    fvgs: List[Level] = []
    for i in range(1, len(candles) - 1):
        prev_c = candles[i - 1]
        next_c = candles[i + 1]

        if next_c["low"] > prev_c["high"]:
            gap_low, gap_high = prev_c["high"], next_c["low"]
            fvgs.append({
                "type": "Fair Value Gap Bullish",
                "price": round((gap_low + gap_high) / 2, 8),
                "low": gap_low, "high": gap_high,
                "tf": tf,
                "strength": _gap_strength(candles, gap_low, gap_high),
            })
        elif next_c["high"] < prev_c["low"]:
            gap_low, gap_high = next_c["high"], prev_c["low"]
            fvgs.append({
                "type": "Fair Value Gap Bearish",
                "price": round((gap_low + gap_high) / 2, 8),
                "low": gap_low, "high": gap_high,
                "tf": tf,
                "strength": _gap_strength(candles, gap_low, gap_high),
            })
    return fvgs


# ─────────────────────────────────────────────────────────────────────────────
# Liquidity zones (equal highs / equal lows)
# ─────────────────────────────────────────────────────────────────────────────

def _cluster_equal(points: List[Dict], label: str, tolerance_pct: float, tf: str) -> List[Level]:
    if len(points) < 2:
        return []
    sorted_points = sorted(points, key=lambda p: p["price"])
    clusters: List[List[Dict]] = []
    current = [sorted_points[0]]
    for p in sorted_points[1:]:
        ref = current[0]["price"]
        within_tolerance = ref != 0 and abs(p["price"] - ref) / abs(ref) * 100 <= tolerance_pct
        if within_tolerance:
            current.append(p)
        else:
            if len(current) >= 2:
                clusters.append(current)
            current = [p]
    if len(current) >= 2:
        clusters.append(current)

    result = []
    for cluster in clusters:
        prices = [p["price"] for p in cluster]
        avg = sum(prices) / len(prices)
        result.append({
            "type": label,
            "price": round(avg, 8),
            "low": min(prices),
            "high": max(prices),
            "tf": tf,
            "strength": min(10, max(1, len(cluster) * 2)),
        })
    return result


def detect_liquidity_zones(
    candles: List[Candle],
    swings: Optional[List[Dict]] = None,
    tolerance_pct: float = 0.1,
    tf: str = "1H",
) -> List[Level]:
    """Equal highs (buy-side liquidity) / equal lows (sell-side liquidity) from swing clusters."""
    if swings is None:
        swings = detect_swings(candles)
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    zones = []
    zones += _cluster_equal(highs, "Equal Highs", tolerance_pct, tf)
    zones += _cluster_equal(lows, "Equal Lows", tolerance_pct, tf)
    return zones


# ─────────────────────────────────────────────────────────────────────────────
# Premium / Discount
# ─────────────────────────────────────────────────────────────────────────────

def premium_discount_zone(candles: List[Candle]) -> Dict:
    """Current price position relative to the fib-0.5 equilibrium of the candle range."""
    if not candles:
        return {"equilibrium": None, "zone": "unknown", "range_low": None, "range_high": None}

    range_high = max(c["high"] for c in candles)
    range_low = min(c["low"] for c in candles)
    equilibrium = (range_high + range_low) / 2
    current_price = candles[-1]["close"]

    if current_price > equilibrium:
        zone = "premium"
    elif current_price < equilibrium:
        zone = "discount"
    else:
        zone = "equilibrium"

    return {
        "equilibrium": round(equilibrium, 8),
        "zone": zone,
        "range_low": range_low,
        "range_high": range_high,
        "current_price": current_price,
    }


def premium_discount_levels(candles: List[Candle], tf: str = "1H") -> List[Level]:
    pd = premium_discount_zone(candles)
    if pd["equilibrium"] is None or pd["zone"] == "equilibrium":
        return []

    if pd["zone"] == "premium":
        return [{
            "type": "Premium Zone", "price": None,
            "low": pd["equilibrium"], "high": pd["range_high"],
            "tf": tf, "strength": 5,
        }]
    return [{
        "type": "Discount Zone", "price": None,
        "low": pd["range_low"], "high": pd["equilibrium"],
        "tf": tf, "strength": 5,
    }]


# ─────────────────────────────────────────────────────────────────────────────
# MTF confluence scoring
# ─────────────────────────────────────────────────────────────────────────────

def _tf_bias(candles: List[Candle]) -> str:
    """bullish/bearish/neutral — last BOS/CHoCH direction, else HH/HL vs LH/LL fallback."""
    events = detect_bos_choch(candles)
    if events:
        return events[-1]["direction"]
    if len(candles) >= 3:
        highs = [c["high"] for c in candles[-3:]]
        lows = [c["low"] for c in candles[-3:]]
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            return "bullish"
        if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            return "bearish"
    return "neutral"


def score_confluence(candles_by_tf: Dict[str, List[Candle]]) -> Dict:
    """
    Weight each TF's structural bias (1D=0.4, 4H=0.3, 1H=0.2, 15m=0.1).
    Score = weighted agreement with the highest-TF non-neutral bias, 0-100.
    """
    biases: Dict[str, str] = {}
    factors: Dict[str, str] = {}
    for tf, candles in candles_by_tf.items():
        if not candles:
            continue
        bias = _tf_bias(candles)
        biases[tf] = bias
        factors[f"structure_{tf}"] = bias

    if not biases:
        return {"score": 0, "factors": {}, "conflicts": ["No candle data"]}

    primary = next(
        (biases[tf] for tf in _TF_PRIORITY if tf in biases and biases[tf] != "neutral"),
        "neutral",
    )

    conflicts: List[str] = []
    weighted_sum = 0.0
    total_weight = 0.0
    for tf, bias in biases.items():
        weight = _TF_WEIGHTS.get(tf, 0.1)
        total_weight += weight
        if bias == "neutral":
            weighted_sum += weight * 0.5
        elif bias == primary:
            weighted_sum += weight
        else:
            conflicts.append(f"{tf} structure ({bias}) conflicts with primary bias ({primary})")

    score = int(round((weighted_sum / total_weight) * 100)) if total_weight else 0
    return {"score": max(0, min(100, score)), "factors": factors, "conflicts": conflicts}


# ─────────────────────────────────────────────────────────────────────────────
# Pre-filter — "is there anything worth spending an API call on?"
# ─────────────────────────────────────────────────────────────────────────────

def has_structural_setup(smc_levels: Dict, min_score: int = 35, min_levels: int = 1) -> bool:
    key_levels = smc_levels.get("key_levels", [])
    confluence = smc_levels.get("confluence", {})

    structural_count = sum(1 for lvl in key_levels if lvl.get("type") in _STRUCTURAL_TYPES)
    score_ok = confluence.get("score", 0) >= min_score

    return structural_count >= min_levels and score_ok


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def build_smc_levels(candles_by_tf: Dict[str, List[Candle]]) -> Dict:
    """
    Run every detector per timeframe and assemble the smc_levels dict
    consumed by claude_service.build_prompt() (key: "smc_levels").
    """
    key_levels: List[Level] = []

    for tf, candles in candles_by_tf.items():
        if not candles:
            continue
        swings = detect_swings(candles)
        bos_events = detect_bos_choch(candles, swings, tf=tf)

        key_levels += detect_order_blocks(candles, bos_events, tf=tf)
        key_levels += detect_fair_value_gaps(candles, tf=tf)
        key_levels += detect_liquidity_zones(candles, swings, tf=tf)
        key_levels += premium_discount_levels(candles, tf=tf)
        key_levels += [
            {
                "type": e["type"], "price": e["price"],
                "low": None, "high": None,
                "tf": tf, "strength": e["strength"],
            }
            for e in bos_events
        ]

    confluence = score_confluence(candles_by_tf)

    return {
        "key_levels": key_levels,
        "confluence": confluence,
    }
