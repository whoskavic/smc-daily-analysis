"""
Snapshot enrichment layer — Phase 2 integration point.

Wraps binance_service.fetch_market_snapshot() and adds the Phase 2 fields
(candles_15m, smc_levels, kill_zone) that claude_service.build_prompt()
already knows how to consume. binance_service.py and claude_service.py are
both left untouched; this module is the seam between them.
"""
from __future__ import annotations

from typing import Dict

from app.services.binance_service import fetch_market_snapshot, fetch_ohlcv
from app.services.session_utils import current_session
from app.services import smc_engine

CANDLES_15M_LIMIT = 96  # ~24 hours of 15m bars


def build_enriched_snapshot(symbol: str) -> Dict:
    """Fetch the canonical snapshot, then enrich it with Phase 2 SMC pre-computation."""
    snapshot = fetch_market_snapshot(symbol)

    candles_15m = fetch_ohlcv(symbol, "15m", limit=CANDLES_15M_LIMIT)
    snapshot["candles_15m"] = candles_15m

    candles_by_tf = {
        "1D": snapshot.get("candles_1d", []),
        "4H": snapshot.get("candles_4h", []),
        "1H": snapshot.get("candles_1h", []),
        "15m": candles_15m,
    }
    snapshot["smc_levels"] = smc_engine.build_smc_levels(candles_by_tf)
    snapshot["kill_zone"] = current_session()

    return snapshot


def has_setup(snapshot: Dict) -> bool:
    """Pre-filter gate: does this snapshot have a structural setup worth a Claude call?"""
    return smc_engine.has_structural_setup(snapshot.get("smc_levels", {}))
