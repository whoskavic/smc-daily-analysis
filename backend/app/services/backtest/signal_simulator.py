"""
Signal simulation for backtesting — Phase 4.

Default mode: rule-based decision using the same gates as the live Claude
system prompt (>=85 confidence, minimum 1:3 RR) but scored from
smc_engine.score_confluence() + nearest structural zone instead of an LLM
call — a full historical backtest runs in seconds instead of costing
thousands of Claude API calls.

Optional cross-check: a small random sample of qualifying bars can be
replayed through the real claude_service.run_analysis() (claude_service.py
untouched) to sanity-check the rule-based approximation against actual
model output. Off by default (claude_sample_pct=0) — see engine.run_backtest().
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional

MIN_CONFIDENCE = 85
MIN_RR = 3.0
SL_BUFFER_PCT = 0.15  # extra room beyond the OB/FVG edge, as a fraction of zone size


def _nearest_zone(key_levels: List[Dict], price: float, direction: str) -> Optional[Dict]:
    """Closest OB/FVG in the trade's direction — support below price for LONG,
    resistance above price for SHORT."""
    wanted_types = (
        {"Order Block Bullish", "Fair Value Gap Bullish"}
        if direction == "LONG"
        else {"Order Block Bearish", "Fair Value Gap Bearish"}
    )
    candidates = [
        lvl for lvl in key_levels
        if lvl.get("type") in wanted_types and lvl.get("low") is not None and lvl.get("high") is not None
    ]
    if not candidates:
        return None

    def distance(lvl):
        mid = (lvl["low"] + lvl["high"]) / 2
        if direction == "LONG":
            return (price - mid) if mid <= price else float("inf")
        return (mid - price) if mid >= price else float("inf")

    candidates.sort(key=distance)
    best = candidates[0]
    return best if distance(best) != float("inf") else None


def _liquidity_target(key_levels: List[Dict], price: float, direction: str) -> Optional[float]:
    """Nearest Equal Highs/Lows beyond price in trade direction — candidate TP2."""
    label = "Equal Highs" if direction == "LONG" else "Equal Lows"
    zones = [lvl for lvl in key_levels if lvl.get("type") == label and lvl.get("price") is not None]
    if not zones:
        return None
    if direction == "LONG":
        beyond = [z["price"] for z in zones if z["price"] > price]
        return min(beyond) if beyond else None
    beyond = [z["price"] for z in zones if z["price"] < price]
    return max(beyond) if beyond else None


def _no_trade(score: int, reason: str) -> Dict:
    return {
        "decision": "NO_TRADE", "direction": None, "entry_price": None,
        "stop_loss": None, "tp1": None, "tp2": None,
        "confidence": score, "rr_ratio": None, "no_trade_reason": reason,
    }


def rule_based_signal(smc_levels: Dict, current_price: float) -> Dict:
    """
    Deterministic analog of claude_service's execution decision: same
    85-confidence / 1:3-RR gates, driven by score_confluence() + nearest
    structural zone instead of an LLM call.
    """
    key_levels = smc_levels.get("key_levels", [])
    confluence = smc_levels.get("confluence", {})
    score = confluence.get("score", 0)
    conflicts = confluence.get("conflicts", [])
    factors = confluence.get("factors", {})

    primary_bias = "neutral"
    for tf in ("structure_1D", "structure_4H", "structure_1H", "structure_15m"):
        if factors.get(tf) not in (None, "neutral"):
            primary_bias = factors[tf]
            break

    if primary_bias == "neutral" or score < MIN_CONFIDENCE:
        return _no_trade(score, "Confluence score/bias below TRADE threshold")

    if conflicts:
        return _no_trade(score, f"Confluence conflict present: {conflicts[0]}")

    direction = "LONG" if primary_bias == "bullish" else "SHORT"
    zone = _nearest_zone(key_levels, current_price, direction)
    if zone is None:
        return _no_trade(score, "No structural OB/FVG zone found in bias direction")

    zone_low, zone_high = zone["low"], zone["high"]
    zone_size = max(zone_high - zone_low, current_price * 0.0005)
    buffer = zone_size * SL_BUFFER_PCT

    if direction == "LONG":
        entry, stop_loss = zone_high, zone_low - buffer
        risk = entry - stop_loss
        tp1 = entry + risk * MIN_RR
    else:
        entry, stop_loss = zone_low, zone_high + buffer
        risk = stop_loss - entry
        tp1 = entry - risk * MIN_RR

    if risk <= 0:
        return _no_trade(score, "Invalid risk distance derived from zone")

    liquidity_tp = _liquidity_target(key_levels, entry, direction)
    tp2 = liquidity_tp if liquidity_tp is not None else tp1
    if direction == "LONG" and tp2 < tp1:
        tp2 = tp1
    if direction == "SHORT" and tp2 > tp1:
        tp2 = tp1

    rr = abs(tp1 - entry) / risk

    return {
        "decision": "TRADE",
        "direction": direction,
        "entry_price": round(entry, 8),
        "stop_loss": round(stop_loss, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "confidence": score,
        "rr_ratio": round(rr, 2),
        "no_trade_reason": None,
    }


def claude_sample_signal(bar_snapshot: Dict) -> Dict:
    """Real Claude API call for a sampled bar — cross-check only, not the
    main backtest loop. Reuses claude_service.py unchanged."""
    from app.services import claude_service
    result = claude_service.run_analysis(bar_snapshot)
    return result["execution"]


def should_sample(claude_sample_pct: float) -> bool:
    return claude_sample_pct > 0 and random.random() < claude_sample_pct
