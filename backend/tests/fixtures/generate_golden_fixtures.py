"""
Golden-fixture generator for the smc_engine.py SmcConfig PR — run ONCE against
an unmodified master (before SmcConfig existed) to freeze master's actual
output as a regression fixture.

Produces two kinds of files under tests/fixtures/:
  - golden_candles.json     — the deterministic synthetic input (candles_by_tf)
  - golden_<fn>.json         — that unmodified master's <fn>(...) returned for it

tests/test_phase6_variants.py loads golden_candles.json (not this generator)
and asserts the post-SmcConfig code, called with default SmcConfig(), still
reproduces golden_<fn>.json exactly. This script is not re-run as part of the
test suite — it is a one-time snapshot tool, kept for reproducibility/audit.

Uses only Python's stdlib `random` (seeded) — no numpy — so the candle
generation itself can never drift with a numpy version change.
"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, BACKEND)

from app.services import smc_engine  # noqa: E402

FIXTURES_DIR = os.path.dirname(os.path.abspath(__file__))
SEED = 20260101
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _gen_series(seed: int, n: int, step: timedelta, start_price: float) -> list:
    """Deterministic seeded random walk with realistic OHLC ordering
    (high >= max(open,close), low <= min(open,close)) and enough drift
    reversals to produce swings, BOS/CHoCH, order blocks, FVGs and equal
    highs/lows — i.e. non-trivial output from every detector."""
    rng = random.Random(seed)
    candles = []
    price = start_price
    ts = START
    # A few deliberate drift-direction segments (not pure noise) so the walk
    # actually trends and reverses, instead of just chopping around flat.
    segment_len = max(3, n // 6)
    for i in range(n):
        segment = i // segment_len
        drift = 0.006 if segment % 2 == 0 else -0.006
        change_pct = drift + rng.uniform(-0.01, 0.01)
        open_p = price
        close_p = max(0.5, open_p * (1 + change_pct))
        wick_up = abs(rng.uniform(0, 0.006))
        wick_down = abs(rng.uniform(0, 0.006))
        high_p = max(open_p, close_p) * (1 + wick_up)
        low_p = min(open_p, close_p) * (1 - wick_down)
        volume = round(rng.uniform(100.0, 1000.0), 3)
        candles.append({
            "timestamp": ts.isoformat(),
            "open": round(open_p, 6), "high": round(high_p, 6),
            "low": round(low_p, 6), "close": round(close_p, 6),
            "volume": volume,
        })
        price = close_p
        ts += step
    return candles


def build_golden_candles() -> dict:
    return {
        "1D": _gen_series(SEED + 1, 60, timedelta(days=1), 100.0),
        "4H": _gen_series(SEED + 2, 80, timedelta(hours=4), 100.0),
        "1H": _gen_series(SEED + 3, 100, timedelta(hours=1), 100.0),
        "15m": _gen_series(SEED + 4, 150, timedelta(minutes=15), 100.0),
    }


def main() -> None:
    candles_by_tf = build_golden_candles()
    with open(os.path.join(FIXTURES_DIR, "golden_candles.json"), "w") as f:
        json.dump(candles_by_tf, f, indent=2)

    candles_1h = candles_by_tf["1H"]

    outputs = {
        "golden_bos_choch.json": smc_engine.detect_bos_choch(candles_1h, tf="1H"),
        "golden_order_blocks.json": smc_engine.detect_order_blocks(candles_1h, tf="1H"),
        "golden_tf_bias.json": {"bias": smc_engine._tf_bias(candles_1h)},
        "golden_confluence.json": smc_engine.score_confluence(candles_by_tf),
        "golden_smc_levels.json": smc_engine.build_smc_levels(candles_by_tf),
    }
    for filename, value in outputs.items():
        with open(os.path.join(FIXTURES_DIR, filename), "w") as f:
            json.dump(value, f, indent=2)
        print(f"wrote {filename}")

    print(f"1H bos_choch events: {len(outputs['golden_bos_choch.json'])}")
    print(f"1H order_blocks: {len(outputs['golden_order_blocks.json'])}")
    print(f"1H tf_bias: {outputs['golden_tf_bias.json']['bias']}")
    print(f"confluence score: {outputs['golden_confluence.json']['score']}")
    print(f"smc_levels key_levels count: {len(outputs['golden_smc_levels.json']['key_levels'])}")


if __name__ == "__main__":
    main()
