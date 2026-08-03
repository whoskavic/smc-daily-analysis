"""
Walk-forward SMC replay for backtesting — Phase 4.

At each 15m step, builds the exact same fixed-window snapshot shape that
snapshot_builder.build_enriched_snapshot() produces live — 1D:30, 4H:48,
1H:24, 15m:96 candles — using only candles with timestamp <= the current
bar's close. No lookahead. Reuses smc_engine.py and session_utils.py
unchanged, so backtest structure detection is identical to live/paper
trading; only the data window is historical instead of "now".

Fixed-size rolling windows (matching live) are what keep this walk-forward
loop cheap: each step re-derives SMC levels from a small, constant-size
slice per timeframe rather than an ever-growing history, so replaying
months of 15m data stays roughly linear in bar count.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Generator, List

from app.services import smc_engine
from app.services.session_utils import current_session

WINDOW_1D = 30
WINDOW_4H = 48
WINDOW_1H = 24
WINDOW_15M = 96

# Skip until there's enough history for the widest window to be meaningful.
DEFAULT_WARMUP_BARS = WINDOW_1D * 4


def _window_by_timestamp(candles: List[Dict], as_of: str, size: int) -> List[Dict]:
    """Candles with timestamp <= as_of (already-closed bars only), last `size`."""
    closed = [c for c in candles if c["timestamp"] <= as_of]
    return closed[-size:]


def replay(
    candles_15m: List[Dict],
    candles_1h: List[Dict],
    candles_4h: List[Dict],
    candles_1d: List[Dict],
    warmup_bars: int = DEFAULT_WARMUP_BARS,
) -> Generator[Dict, None, None]:
    """
    Yield one snapshot dict per 15m bar, walk-forward, no lookahead.

    Each yielded dict mirrors snapshot_builder.build_enriched_snapshot()'s
    shape (candles_1d/4h/1h/15m, smc_levels, kill_zone), plus `bar_index`,
    `timestamp`, and OHLC of the current bar for the simulator/engine.
    """
    if len(candles_15m) <= warmup_bars:
        return

    for i in range(warmup_bars, len(candles_15m)):
        bar = candles_15m[i]
        as_of = bar["timestamp"]

        window_15m = _window_by_timestamp(candles_15m, as_of, WINDOW_15M)
        window_1h = _window_by_timestamp(candles_1h, as_of, WINDOW_1H)
        window_4h = _window_by_timestamp(candles_4h, as_of, WINDOW_4H)
        window_1d = _window_by_timestamp(candles_1d, as_of, WINDOW_1D)

        candles_by_tf = {
            "1D": window_1d, "4H": window_4h, "1H": window_1h, "15m": window_15m,
        }
        smc_levels = smc_engine.build_smc_levels(candles_by_tf)
        kill_zone = current_session(datetime.fromisoformat(as_of))

        yield {
            "bar_index": i,
            "timestamp": as_of,
            "open_price": bar["open"],
            "high_price": bar["high"],
            "low_price": bar["low"],
            "close_price": bar["close"],
            "candles_1d": window_1d,
            "candles_4h": window_4h,
            "candles_1h": window_1h,
            "candles_15m": window_15m,
            "smc_levels": smc_levels,
            "kill_zone": kill_zone,
        }
