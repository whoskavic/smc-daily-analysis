"""
Walk-forward SMC replay for backtesting — Phase 4, fidelity-corrected Phase 6.

At each 15m step, builds the exact same fixed-window snapshot shape that
snapshot_builder.build_enriched_snapshot() produces live — 1D:30, 4H:48,
1H:24, 15m:96 candles. Reuses smc_engine.py and session_utils.py unchanged,
so backtest structure detection is identical to live/paper trading; only the
data window is historical instead of "now".

No-lookahead rule: the snapshot for 15m bar `i` (open time `t`) represents
the moment that bar CLOSES, i.e. `as_of_close = t + 15m` — the earliest
instant a live system could actually have acted on it. ccxt/exchange
timestamps are candle OPEN times, so for each higher timeframe (1h/4h/1d):

  - A source HTF candle is only "closed" (and usable as-is) once
    `open + duration <= as_of_close`. The HTF candle whose period contains
    `t` has NOT closed yet at `as_of_close` and must not be used with its
    (future-complete) OHLC — that was the original lookahead bug.
  - Instead, that still-forming HTF candle is rebuilt from only the 15m
    candles seen so far within its period (`[period_open, t]`), exactly
    mirroring what a live system's partially-filled current bar looks like.

Fixed-size rolling windows (matching live) are what keep this walk-forward
loop cheap: each step re-derives SMC levels from a small, constant-size
slice per timeframe rather than an ever-growing history. Closed-candle
membership and the 15m window are both located via bisect over
precomputed epoch-ms open times, computed once per replay() call, so no
bar does a full linear scan of its source candle list.
"""
from __future__ import annotations

import bisect
from datetime import datetime, timezone
from typing import Dict, Generator, List

from app.services import smc_engine
from app.services.session_utils import current_session

WINDOW_1D = 30
WINDOW_4H = 48
WINDOW_1H = 24
WINDOW_15M = 96

# Skip until there's enough history for the widest window to be meaningful.
DEFAULT_WARMUP_BARS = WINDOW_1D * 4

_BAR_MS = 15 * 60 * 1000
_HTF_SPECS = {
    # name: (duration_ms, window_size)
    "1H": (60 * 60 * 1000, WINDOW_1H),
    "4H": (4 * 60 * 60 * 1000, WINDOW_4H),
    "1D": (24 * 60 * 60 * 1000, WINDOW_1D),
}


def _epoch_ms(timestamp: str) -> int:
    dt = datetime.fromisoformat(timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _window_by_timestamp(candles: List[Dict], as_of: str, size: int) -> List[Dict]:
    """Candles with timestamp <= as_of, last `size`. Simple/generic fallback
    kept for compatibility; the replay loop itself uses the bisect-indexed
    path below for performance."""
    closed = [c for c in candles if c["timestamp"] <= as_of]
    return closed[-size:]


def _aggregate_forming_candle(candles_15m_slice: List[Dict], period_open_ms: int) -> Dict:
    """Build the still-forming HTF candle from the 15m bars observed so far
    within its period — open of the first, high/low extremes, close of the
    latest, volume summed. Same dict shape as a normal fetched candle."""
    return {
        "timestamp": _iso_from_ms(period_open_ms),
        "open": candles_15m_slice[0]["open"],
        "high": max(c["high"] for c in candles_15m_slice),
        "low": min(c["low"] for c in candles_15m_slice),
        "close": candles_15m_slice[-1]["close"],
        "volume": sum(c["volume"] for c in candles_15m_slice),
    }


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

    fifteen_opens_ms = [_epoch_ms(c["timestamp"]) for c in candles_15m]
    htf_sources = {
        "1H": (candles_1h, [_epoch_ms(c["timestamp"]) for c in candles_1h]),
        "4H": (candles_4h, [_epoch_ms(c["timestamp"]) for c in candles_4h]),
        "1D": (candles_1d, [_epoch_ms(c["timestamp"]) for c in candles_1d]),
    }

    for i in range(warmup_bars, len(candles_15m)):
        bar = candles_15m[i]
        as_of = bar["timestamp"]
        t_ms = fifteen_opens_ms[i]
        as_of_close_ms = t_ms + _BAR_MS

        window_15m = candles_15m[max(0, i + 1 - WINDOW_15M):i + 1]

        htf_windows: Dict[str, List[Dict]] = {}
        for name, (duration_ms, size) in _HTF_SPECS.items():
            candles, opens_ms = htf_sources[name]
            cutoff_ms = as_of_close_ms - duration_ms
            closed_count = bisect.bisect_right(opens_ms, cutoff_ms)
            closed = candles[max(0, closed_count - size):closed_count]

            period_open_ms = (t_ms // duration_ms) * duration_ms
            period_closed = (period_open_ms + duration_ms) <= as_of_close_ms

            if period_closed:
                combined = closed
            else:
                start_idx = bisect.bisect_left(fifteen_opens_ms, period_open_ms)
                forming_slice = candles_15m[start_idx:i + 1]
                forming = _aggregate_forming_candle(forming_slice, period_open_ms)
                combined = closed + [forming]

            htf_windows[name] = combined[-size:]

        candles_by_tf = {
            "1D": htf_windows["1D"], "4H": htf_windows["4H"],
            "1H": htf_windows["1H"], "15m": window_15m,
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
            "candles_1d": htf_windows["1D"],
            "candles_4h": htf_windows["4H"],
            "candles_1h": htf_windows["1H"],
            "candles_15m": window_15m,
            "smc_levels": smc_levels,
            "kill_zone": kill_zone,
        }
