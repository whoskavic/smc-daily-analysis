"""
Backtest execution engine — Phase 4, proxy realism + decision cadence (Phase 6).

Orchestrates: fetch historical OHLCV (data_loader) → walk-forward SMC replay
(smc_replay, reusing smc_engine.py unchanged) → decision-schedule gating
(every 15m bar, or once/day matching live's run_daily_analysis cadence) →
rule-based signal generation (signal_simulator, with optional Claude-sample
cross-check) → event-driven trade simulation (trade_simulator) or, for
comparison, the original vectorbt portfolio simulation.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import vectorbt as vbt

from app.services import smc_engine
from app.services.smc_engine import SmcConfig
from app.services.backtest import data_loader, smc_replay, signal_simulator, trade_simulator
from app.services.backtest.signal_simulator import SignalConfig
from app.services.backtest.trade_simulator import SimConfig

logger = logging.getLogger(__name__)

DEFAULT_FEES_PCT = 0.0004       # sim_mode="vbt_legacy" only — event mode uses SimConfig maker/taker fees
DEFAULT_SLIPPAGE_PCT = 0.0005
MIN_BARS_REQUIRED = 200

_SL_BUCKETS = (
    ("<0.1%", lambda p: p < 0.1),
    ("0.1-0.3%", lambda p: 0.1 <= p < 0.3),
    ("0.3-0.6%", lambda p: 0.3 <= p < 0.6),
    (">=0.6%", lambda p: p >= 0.6),
)
_NEUTRAL_DIAGNOSTICS = {
    "sl_pct_stats": None,
    "cost_r_stats": None,
    "fill_bar_sl_exits": 0,
    "r_by_sl_bucket": {label: {"count": 0, "expectancy_r": 0.0} for label, _ in _SL_BUCKETS},
}

# Each timeframe is fetched from `since - lookback` instead of exactly
# `since`, so smc_replay's rolling HTF windows (smc_replay.WINDOW_*) are
# already closed-or-forming — no lookahead — by the time the walk-forward
# reaches the first in-range bar, matching what live always sees (its
# snapshot windows are never "cold"). Derived from smc_replay's own window
# constants so the two can't drift apart; the added margin covers weekend/
# exchange-downtime gaps and the forming-candle aggregation at the boundary.
_LOOKBACK_DELTAS = {
    "1d": timedelta(days=smc_replay.WINDOW_1D + 2),
    "4h": timedelta(hours=smc_replay.WINDOW_4H * 4) + timedelta(days=1),
    "1h": timedelta(hours=smc_replay.WINDOW_1H) + timedelta(days=1),
    "15m": timedelta(minutes=smc_replay.WINDOW_15M * 15) + timedelta(days=1),
}

# The lookback span a timeframe actually needs to fill its replay window —
# _LOOKBACK_DELTAS minus the safety margin — used only to judge whether a
# fetch came back "complete" (see _tf_lookback_status). One bar of tolerance
# absorbs exchange/cache boundary rounding.
_REQUIRED_SPAN = {
    "1d": timedelta(days=smc_replay.WINDOW_1D),
    "4h": timedelta(hours=smc_replay.WINDOW_4H * 4),
    "1h": timedelta(hours=smc_replay.WINDOW_1H),
    "15m": timedelta(minutes=smc_replay.WINDOW_15M * 15),
}
_TF_BAR_DURATION = {
    "1d": timedelta(days=1), "4h": timedelta(hours=4),
    "1h": timedelta(hours=1), "15m": timedelta(minutes=15),
}


def _tf_lookback_status(candles: List[Dict], tf: str, since: datetime) -> Dict:
    """Whether `candles` actually reaches back far enough to fill `tf`'s
    full replay window (WINDOW_* bars, no safety margin — one bar of
    tolerance) before `since`. Checked per timeframe because
    data_loader's cache accepts a >=95%-coverage hit: a cache holding only
    the in-range portion (no real lookback) can still pass that check for a
    long-enough backtest, so a single overall completeness signal isn't
    reliable — each timeframe's own earliest candle must be verified."""
    if not candles:
        return {"earliest": None, "complete": False}
    earliest = datetime.fromisoformat(candles[0]["timestamp"])
    required_earliest = since - _REQUIRED_SPAN[tf] + _TF_BAR_DURATION[tf]
    return {"earliest": earliest.isoformat(), "complete": earliest <= required_earliest}


def _fetch_tf_with_lookback(symbol: str, tf: str, since: datetime, until: datetime) -> tuple:
    """Fetches one timeframe from `since - lookback`; if the result doesn't
    actually reach back far enough (stale/partial cache, or a genuinely
    cold cache), refetches once with use_cache=False before giving up."""
    candles = data_loader.fetch_historical_ohlcv(symbol, tf, since - _LOOKBACK_DELTAS[tf], until)
    status = _tf_lookback_status(candles, tf, since)
    if not status["complete"]:
        candles = data_loader.fetch_historical_ohlcv(
            symbol, tf, since - _LOOKBACK_DELTAS[tf], until, use_cache=False,
        )
        status = _tf_lookback_status(candles, tf, since)
        if not status["complete"]:
            logger.warning(
                f"[Backtest] {symbol} {tf}: insufficient lookback history before "
                f"{since.isoformat()} (earliest={status['earliest']}) even after a "
                f"fresh exchange pull — that timeframe's replay window may start partially filled"
            )
    return candles, status


def _fetch_with_lookback(symbol: str, since: datetime, until: datetime) -> Dict:
    """
    Fetches 15m/1h/4h/1d from `since - lookback` (see _LOOKBACK_DELTAS) so
    smc_replay's HTF windows are already full by the time the walk-forward
    reaches the first bar with timestamp >= since. Everything downstream of
    the replay loop (decisions, signals, trades, equity curve, metrics) is
    still scoped to bars >= since — the extra history is context only, used
    to warm up smc_replay's rolling windows.

    Each timeframe's lookback is verified independently (_tf_lookback_status)
    and refetched once bypassing the cache if short — data_loader's cache
    accepts a >=95%-coverage hit, which can silently return a slice with no
    real lookback on a long-enough backtest. If a timeframe still falls
    short after that (the exchange genuinely lacks the history — a
    freshly-listed symbol, or `since` at the start of available history),
    its status stays incomplete and a warning names it.

    Returns a dict: candles_15m/1h/4h/1d, warmup_bars (the candles_15m index
    of the first bar with timestamp >= since — computed from whatever 15m
    history is available, even if short of a full window), lookback_start
    (ISO, the 15m fetch's actual earliest candle), lookback_complete (True
    iff every timeframe's lookback is complete), lookback_by_tf (per-
    timeframe {"earliest": iso|None, "complete": bool}).
    """
    candles_by_tf: Dict[str, List[Dict]] = {}
    lookback_by_tf: Dict[str, Dict] = {}
    for tf in ("15m", "1h", "4h", "1d"):
        candles_by_tf[tf], lookback_by_tf[tf] = _fetch_tf_with_lookback(symbol, tf, since, until)

    candles_15m = candles_by_tf["15m"]
    earliest_15m = datetime.fromisoformat(candles_15m[0]["timestamp"]) if candles_15m else None
    has_any_lookback = earliest_15m is not None and earliest_15m < since

    if has_any_lookback:
        warmup_bars = next(
            (idx for idx, c in enumerate(candles_15m) if datetime.fromisoformat(c["timestamp"]) >= since),
            len(candles_15m),
        )
        lookback_start = earliest_15m.isoformat()
    else:
        warmup_bars = min(smc_replay.DEFAULT_WARMUP_BARS, len(candles_15m))
        lookback_start = since.isoformat()

    return {
        "candles_15m": candles_by_tf["15m"], "candles_1h": candles_by_tf["1h"],
        "candles_4h": candles_by_tf["4h"], "candles_1d": candles_by_tf["1d"],
        "warmup_bars": warmup_bars,
        "lookback_start": lookback_start,
        "lookback_complete": all(s["complete"] for s in lookback_by_tf.values()),
        "lookback_by_tf": lookback_by_tf,
    }


def run_backtest(
    symbol: str,
    since: datetime,
    until: Optional[datetime] = None,
    init_cash: float = 1000.0,
    fees_pct: float = DEFAULT_FEES_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    claude_sample_pct: float = 0.0,
    claude_sample_max: int = 0,
    sim_mode: str = "event",
    sim_config: Optional[SimConfig] = None,
    signal_config: Optional[SignalConfig] = None,
    decision_schedule: str = "every_bar",
    smc_config: Optional[SmcConfig] = None,
) -> Dict:
    """
    Full backtest run for one symbol over [since, until).

    Args:
        symbol: e.g. "BTC/USDT"
        since / until: UTC datetime range (until defaults to now)
        init_cash: virtual starting balance, USDT
        fees_pct: per-side fee — sim_mode="vbt_legacy" only. Event mode's
            fees come from sim_config.maker_fee_pct / taker_fee_pct.
        slippage_pct: per-side, as a fraction (0.0005 = 5bps)
        claude_sample_pct: 0-1, random fraction of decision bars to
            additionally cross-check against a real Claude API call
            (0 = disabled, default). Mutually exclusive with claude_sample_max.
        claude_sample_max: sample up to this many decision bars — evenly
            spaced and deterministic, including NO_TRADE bars — instead of
            randomly. Mutually exclusive with claude_sample_pct.
        sim_mode: "event" (default) — event-driven trade_simulator, modeling
            live's LIMIT entry + split TP1/TP2 + breakeven SL. "vbt_legacy" —
            the original close-only vectorbt portfolio simulation, kept for
            one release to compare against.
        sim_config: SimConfig for sim_mode="event"; if omitted, one is built
            from init_cash/slippage_pct plus settings.risk_per_trade_pct
            and settings.max_leverage.
        signal_config: SignalConfig (minimum risk-distance floor) for the
            rule-based proxy; if omitted, no floor (current behavior).
        decision_schedule: "every_bar" (default, current behavior) or
            "daily" — one decision per calendar day, on the 15m bar closing
            at settings.daily_analysis_time in settings.timezone, matching
            live's run_daily_analysis cadence (and skipping the structural
            pre-filter there, since live doesn't use one). Replay — and
            bias_by_bar — still runs every bar regardless.
        smc_config: SmcConfig (break_mode/ob_max_scan) for smc_engine's
            detectors; if omitted, smc_engine's own default (today's only
            behavior) is used. Backtest-only — no live/paper-trading caller
            passes this.

    Returns a dict with summary stats, trade list, equity curve, decision-bar
    diagnostics, and (if sampled) a rule-vs-Claude decision comparison — see
    engine tests for the exact shape.
    """
    if sim_mode not in ("event", "vbt_legacy"):
        raise ValueError(f"sim_mode must be 'event' or 'vbt_legacy', got {sim_mode!r}")
    if decision_schedule not in ("every_bar", "daily"):
        raise ValueError(f"decision_schedule must be 'every_bar' or 'daily', got {decision_schedule!r}")
    if claude_sample_pct > 0 and claude_sample_max > 0:
        raise ValueError("claude_sample_pct and claude_sample_max are mutually exclusive")

    if claude_sample_pct > 0 or claude_sample_max > 0:
        from app.config import settings as _settings_check
        key = getattr(_settings_check, "anthropic_api_key", "") or ""
        if not key or key.lower() == "dummy" or len(key) < 20:
            raise ValueError(
                "Claude sampling requested (claude_sample_pct/claude_sample_max) but "
                "ANTHROPIC_API_KEY looks like a placeholder — set a real key in backend/.env"
            )

    signal_config = signal_config or SignalConfig()
    until = until or datetime.now(tz=since.tzinfo)

    fetch = _fetch_with_lookback(symbol, since, until)
    candles_15m = fetch["candles_15m"]
    candles_1h = fetch["candles_1h"]
    candles_4h = fetch["candles_4h"]
    candles_1d = fetch["candles_1d"]
    warmup_bars = fetch["warmup_bars"]
    lookback_start = fetch["lookback_start"]
    lookback_complete = fetch["lookback_complete"]
    lookback_by_tf = fetch["lookback_by_tf"]

    bars_in_range = len(candles_15m) - warmup_bars
    if bars_in_range < MIN_BARS_REQUIRED:
        raise ValueError(
            f"Not enough {symbol} 15m history in range to backtest "
            f"({bars_in_range} bars, need >= {MIN_BARS_REQUIRED})"
        )

    from app.config import settings

    decision_indices = _decision_bar_indices(
        candles_15m, warmup_bars, decision_schedule,
        settings.timezone, settings.daily_analysis_time,
    )
    decision_indices_set = set(decision_indices)
    total_decision_bars = len(decision_indices)

    sample_positions = set()
    if claude_sample_max > 0:
        sample_positions = _evenly_spaced_positions(
            total_decision_bars, min(claude_sample_max, total_decision_bars)
        )

    signals: List[Dict] = []
    sampled: List[Dict] = []
    bias_by_bar: Dict[int, str] = {}
    decision_bar_counter = 0
    consecutive_fallback_failures = 0

    # bar_index below is rebased to be 0-indexed at the first in-range bar
    # (snap["bar_index"] - warmup_bars), so trade_simulator/_simulate_portfolio
    # and bias_by_bar share one consistent indexing with the in-range-only
    # candles array (candles_in_range) they're given below — replay() itself
    # only ever yields bar_index >= warmup_bars, so this is always >= 0.
    for snap in smc_replay.replay(
        candles_15m, candles_1h, candles_4h, candles_1d,
        warmup_bars=warmup_bars, smc_config=smc_config,
    ):
        rel_index = snap["bar_index"] - warmup_bars
        bias_by_bar[rel_index] = signal_simulator.primary_bias(
            snap["smc_levels"].get("confluence", {})
        )

        if snap["bar_index"] not in decision_indices_set:
            continue

        # Live's run_daily_analysis has no structural pre-filter — only the
        # "daily" schedule's decision bars mirror that.
        skip_prefilter = decision_schedule == "daily"
        has_setup = skip_prefilter or smc_engine.has_structural_setup(snap["smc_levels"])

        if has_setup:
            sig = signal_simulator.rule_based_signal(
                snap["smc_levels"], snap["close_price"],
                config=signal_config, candles_15m=snap["candles_15m"],
            )
        else:
            sig = _prefilter_skipped_sig()

        sample_this_bar = False
        if claude_sample_max > 0:
            sample_this_bar = decision_bar_counter in sample_positions
        elif claude_sample_pct > 0:
            sample_this_bar = signal_simulator.should_sample(claude_sample_pct)

        if sample_this_bar:
            record, is_fallback_failure = _run_claude_sample(symbol, snap, sig)
            sampled.append(record)
            consecutive_fallback_failures = consecutive_fallback_failures + 1 if is_fallback_failure else 0
            if len(sampled) == 3 and consecutive_fallback_failures == 3:
                raise ValueError(
                    "First 3 Claude samples all failed with a silent fallback "
                    f"(likely a bad ANTHROPIC_API_KEY or no credits): {record['error']}"
                )

        decision_bar_counter += 1

        if not has_setup or sig["decision"] != "TRADE":
            continue

        signals.append({"bar_index": rel_index, "timestamp": snap["timestamp"], **sig})

    # candles_in_range excludes the lookback warmup region — trades, the
    # equity curve and all metrics are computed on this alone, matching the
    # rebased bar_index used for signals/bias_by_bar above.
    candles_in_range = candles_15m[warmup_bars:]

    if not signals:
        empty_cfg = sim_config or (_default_sim_config(init_cash, slippage_pct) if sim_mode == "event" else None)
        return _empty_result(
            symbol, since, until, len(candles_in_range), sampled, sim_mode, empty_cfg,
            decision_schedule, total_decision_bars, signal_config,
            lookback_start, lookback_complete, lookback_by_tf, smc_config,
        )

    if sim_mode == "vbt_legacy":
        stats = _simulate_portfolio(candles_in_range, signals, init_cash, fees_pct, slippage_pct)
        stats.setdefault("expectancy_r", 0.0)
        stats.setdefault("avg_win_r", 0.0)
        stats.setdefault("avg_loss_r", 0.0)
        stats.setdefault("orders", None)
        stats.update(_NEUTRAL_DIAGNOSTICS)
    else:
        cfg = sim_config or _default_sim_config(init_cash, slippage_pct)
        stats = trade_simulator.simulate(candles_in_range, signals, bias_by_bar, cfg)
        sim_config = cfg
        stats.update(_compute_diagnostics(stats["trades"]))

    stats.update({
        "symbol": symbol,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "lookback_start": lookback_start,
        "lookback_complete": lookback_complete,
        "lookback_by_tf": lookback_by_tf,
        "smc_config": _smc_config_as_dict(smc_config),
        "bars_analyzed": len(candles_in_range),
        "signals_generated": len(signals),
        "claude_sample": sampled,
        "sim_mode": sim_mode,
        "sim_config": _sim_config_as_dict(sim_config) if sim_mode == "event" else None,
        "decision_schedule": decision_schedule,
        "decision_bars": total_decision_bars,
        "signal_config": _signal_config_as_dict(signal_config),
        "claude_vs_proxy": _compute_claude_vs_proxy(sampled),
    })
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Decision schedule
# ─────────────────────────────────────────────────────────────────────────────

def _decision_bar_indices(
    candles_15m: List[Dict], warmup_bars: int, decision_schedule: str,
    tz_name: str, daily_time: str,
) -> List[int]:
    """15m bar indices (into candles_15m) where a decision is evaluated.
    "every_bar": every replayed bar. "daily": the single bar per calendar day
    (in tz_name) whose CLOSE time (open + 15m) is daily_time — timestamps are
    candle OPEN times, so the close, not the open, is what must match live's
    scheduled trigger."""
    n = len(candles_15m)
    if n <= warmup_bars:
        return []
    if decision_schedule == "every_bar":
        return list(range(warmup_bars, n))

    tz = ZoneInfo(tz_name)
    target_hour, target_minute = (int(x) for x in daily_time.split(":"))
    seen_dates = set()
    indices = []
    for i in range(warmup_bars, n):
        bar_open = datetime.fromisoformat(candles_15m[i]["timestamp"])
        bar_close = bar_open + timedelta(minutes=15)
        local_close = bar_close.astimezone(tz)
        if local_close.hour == target_hour and local_close.minute == target_minute:
            d = local_close.date()
            if d not in seen_dates:
                seen_dates.add(d)
                indices.append(i)
    return indices


def _prefilter_skipped_sig(score: int = 0) -> Dict:
    """Shaped like signal_simulator's NO_TRADE, for decision bars where the
    structural pre-filter was bypassed (decision_schedule="daily") and no
    rule signal was ever computed — used only as the "rule side" of a Claude
    sample record."""
    return {
        "decision": "NO_TRADE", "direction": None, "entry_price": None,
        "stop_loss": None, "tp1": None, "tp2": None,
        "confidence": score, "rr_ratio": None,
        "no_trade_reason": "No structural setup (pre-filter)",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Claude calibration sampling
# ─────────────────────────────────────────────────────────────────────────────

def _evenly_spaced_positions(n_total: int, n_sample: int) -> set:
    """Up to n_sample positions in [0, n_total), evenly spaced, deterministic."""
    if n_sample <= 0 or n_total <= 0:
        return set()
    if n_sample >= n_total:
        return set(range(n_total))
    step = n_total / n_sample
    return {int(i * step) for i in range(n_sample)}


def _sl_pct(entry: Optional[float], sl: Optional[float]) -> Optional[float]:
    if entry is None or sl is None or entry == 0:
        return None
    return abs(entry - sl) / entry * 100.0


# claude_service.run_analysis() never raises — on API/parse failure it
# returns a structurally valid NO_TRADE fallback whose no_trade_reason
# starts with this (see claude_service._no_trade_fallback /
# run_analysis's final except-all block). That's a failed sample, not a
# real Claude opinion, and must be treated as one.
_CLAUDE_FALLBACK_MARKER = "Analysis unavailable after"


def _derive_historical_ticker(symbol: str, snap: Dict) -> Dict:
    """
    Reconstructs binance_service.fetch_ticker()'s 24h stats from the last 96
    15m bars (24h) already carried in the snapshot, matching its field
    definitions exactly: high/low are the 24h price extremes, volume is 24h
    base-asset volume (summed candle volume, same unit as live's `volume`
    field), change_pct is the 24h %% change as a percentage number — e.g.
    2.35 means 2.35%%, not the fraction 0.0235 — matching Binance's
    priceChangePercent.
    """
    window = snap["candles_15m"][-96:]
    if not window:
        return {"symbol": symbol, "last": snap["close_price"],
                "high": None, "low": None, "volume": None, "change_pct": None}
    first_open = window[0]["open"]
    change_pct = ((snap["close_price"] - first_open) / first_open * 100.0) if first_open else None
    return {
        "symbol": symbol,
        "last": snap["close_price"],
        "high": max(c["high"] for c in window),
        "low": min(c["low"] for c in window),
        "volume": sum(c["volume"] for c in window),
        "change_pct": change_pct,
    }


def _run_claude_sample(symbol: str, snap: Dict, rule_sig: Dict) -> tuple:
    """
    Cross-check the rule-based proxy against a real Claude call on the same
    historical snapshot. The snapshot mirrors live's
    snapshot_builder.build_enriched_snapshot() key set; funding_rate and
    fear_greed_index can't be reconstructed from historical OHLCV alone and
    are set to None — see the PR report for the full list.

    Returns (record, is_fallback_failure). is_fallback_failure is True only
    when run_analysis() silently fell back to its NO_TRADE placeholder
    (bad key / no credits / persistent parse failure), as opposed to a
    genuine Claude NO_TRADE call or a raised exception — the caller uses it
    to abort early if the first 3 samples all fail this way.
    """
    record = {
        "timestamp": snap["timestamp"],
        "rule_decision": rule_sig.get("decision"),
        "rule_direction": rule_sig.get("direction"),
        "rule_entry": rule_sig.get("entry_price"),
        "rule_sl": rule_sig.get("stop_loss"),
        "rule_tp1": rule_sig.get("tp1"),
        "rule_tp2": rule_sig.get("tp2"),
        "rule_sl_pct": _sl_pct(rule_sig.get("entry_price"), rule_sig.get("stop_loss")),
    }
    try:
        historical_snapshot = {
            "symbol": symbol,
            "ticker": _derive_historical_ticker(symbol, snap),
            "funding_rate": None,
            "fear_greed_index": None,
            "candles_1d": snap["candles_1d"],
            "candles_4h": snap["candles_4h"],
            "candles_1h": snap["candles_1h"],
            "candles_15m": snap["candles_15m"],
            "smc_levels": snap["smc_levels"],
            "kill_zone": snap["kill_zone"],
        }
        claude_exec = signal_simulator.claude_sample_signal(historical_snapshot)

        no_trade_reason = claude_exec.get("no_trade_reason") or ""
        if no_trade_reason.startswith(_CLAUDE_FALLBACK_MARKER):
            record["error"] = no_trade_reason
            return record, True

        rule_decision = rule_sig.get("decision")
        rule_direction = rule_sig.get("direction")
        claude_decision = claude_exec.get("decision")
        claude_direction = claude_exec.get("direction")
        claude_entry = claude_exec.get("entry_price")
        claude_sl = claude_exec.get("stop_loss")
        both_trade = rule_decision == "TRADE" and claude_decision == "TRADE"
        record.update({
            "claude_decision": claude_decision,
            "claude_direction": claude_direction,
            "claude_entry": claude_entry,
            "claude_sl": claude_sl,
            "claude_tp1": claude_exec.get("tp1"),
            "claude_tp2": claude_exec.get("tp2"),
            "claude_confidence": claude_exec.get("confidence"),
            "claude_rr": claude_exec.get("rr_ratio"),
            "claude_sl_pct": _sl_pct(claude_entry, claude_sl),
            "agree_decision": rule_decision == claude_decision,
            "agree_direction": (rule_direction == claude_direction) if both_trade else None,
        })
        return record, False
    except Exception as e:
        logger.warning(f"[Backtest] Claude sample failed at {snap['timestamp']}: {e}")
        record["error"] = str(e)
        return record, False


def _compute_claude_vs_proxy(sampled: List[Dict]) -> Optional[Dict]:
    """Aggregate rule-vs-Claude agreement/sl_pct stats over successful
    (non-error) samples — mirrors the CLI markdown's "Claude vs proxy"
    section so API/programmatic callers get the same numbers."""
    valid = [s for s in sampled if "error" not in s]
    if not valid:
        return None

    rule_trade_sl_pcts = [
        s["rule_sl_pct"] for s in valid
        if s.get("rule_decision") == "TRADE" and s.get("rule_sl_pct") is not None
    ]
    claude_trade_sl_pcts = [
        s["claude_sl_pct"] for s in valid
        if s.get("claude_decision") == "TRADE" and s.get("claude_sl_pct") is not None
    ]
    both_trade = [s for s in valid if s.get("rule_decision") == "TRADE" and s.get("claude_decision") == "TRADE"]
    decision_agree_n = sum(1 for s in valid if s.get("agree_decision"))
    direction_agree_n = sum(1 for s in both_trade if s.get("agree_direction"))
    claude_trade_n = sum(1 for s in valid if s.get("claude_decision") == "TRADE")

    return {
        "rule_sl_pct_stats": _sl_pct_stats(rule_trade_sl_pcts),
        "claude_sl_pct_stats": _sl_pct_stats(claude_trade_sl_pcts),
        "decision_agreement_pct": round(decision_agree_n / len(valid) * 100.0, 2),
        "direction_agreement_pct": (
            round(direction_agree_n / len(both_trade) * 100.0, 2) if both_trade else None
        ),
        "both_trade_n": len(both_trade),
        "claude_trade_n": claude_trade_n,
    }


# ─────────────────────────────────────────────────────────────────────────────
# sim_config / signal_config defaults + serialization
# ─────────────────────────────────────────────────────────────────────────────

def _default_sim_config(init_cash: float, slippage_pct: float) -> SimConfig:
    from app.config import settings
    return SimConfig(
        init_cash=init_cash,
        slippage_pct=slippage_pct,
        risk_pct=getattr(settings, "risk_per_trade_pct", 1.0),
        leverage=getattr(settings, "max_leverage", 10),
    )


def _sim_config_as_dict(cfg: Optional[SimConfig]) -> Optional[Dict]:
    if cfg is None:
        return None
    return {
        "init_cash": cfg.init_cash,
        "maker_fee_pct": cfg.maker_fee_pct,
        "taker_fee_pct": cfg.taker_fee_pct,
        "slippage_pct": cfg.slippage_pct,
        "order_ttl_bars": cfg.order_ttl_bars,
        "cancel_on_tp1_before_fill": cfg.cancel_on_tp1_before_fill,
        "cancel_on_bias_flip": cfg.cancel_on_bias_flip,
        "tp1_fraction": cfg.tp1_fraction,
        "move_sl_to_be_after_tp1": cfg.move_sl_to_be_after_tp1,
        "sizing_mode": cfg.sizing_mode,
        "risk_pct": cfg.risk_pct,
        "fixed_risk_usdt": cfg.fixed_risk_usdt,
        "fixed_margin_usdt": cfg.fixed_margin_usdt,
        "leverage": cfg.leverage,
        "allow_setup_reentry": cfg.allow_setup_reentry,
    }


def _signal_config_as_dict(cfg: SignalConfig) -> Dict:
    return {
        "min_sl_pct": cfg.min_sl_pct,
        "min_sl_atr": cfg.min_sl_atr,
        "atr_period": cfg.atr_period,
    }


def _smc_config_as_dict(cfg: Optional[SmcConfig]) -> Dict:
    cfg = cfg or SmcConfig()
    return {
        "break_mode": cfg.break_mode,
        "ob_max_scan": cfg.ob_max_scan,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics — SL distance / cost realism (event mode only; vbt_legacy
# trades carry no stop_loss/qty/fees/slippage_cost fields to derive these from)
# ─────────────────────────────────────────────────────────────────────────────

def _sl_pct_of_trade(t: Dict) -> Optional[float]:
    return _sl_pct(t.get("entry_price"), t.get("stop_loss"))


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _sl_pct_stats(sl_pcts: List[float]) -> Optional[Dict]:
    if not sl_pcts:
        return None
    s = sorted(sl_pcts)
    return {
        "min": round(s[0], 4),
        "p25": round(_percentile(s, 0.25), 4),
        "median": round(_percentile(s, 0.5), 4),
        "p75": round(_percentile(s, 0.75), 4),
        "max": round(s[-1], 4),
    }


def _cost_r_stats(trades: List[Dict]) -> Optional[Dict]:
    cost_rs = []
    for t in trades:
        qty, entry, sl = t.get("qty"), t.get("entry_price"), t.get("stop_loss")
        fees, slip = t.get("fees"), t.get("slippage_cost")
        if None in (qty, entry, sl, fees, slip):
            continue
        initial_risk_usdt = qty * abs(entry - sl)
        if initial_risk_usdt <= 0:
            continue
        cost_rs.append((fees + slip) / initial_risk_usdt)
    if not cost_rs:
        return None
    s = sorted(cost_rs)
    return {
        "median": round(_percentile(s, 0.5), 4),
        "mean": round(sum(cost_rs) / len(cost_rs), 4),
    }


def _fill_bar_sl_exits(trades: List[Dict]) -> int:
    return sum(1 for t in trades if t.get("outcome") == "sl" and t.get("entry_time") == t.get("exit_time"))


def _r_by_sl_bucket(trades: List[Dict]) -> Dict:
    out = {}
    for label, pred in _SL_BUCKETS:
        bucket = [t for t in trades if (sp := _sl_pct_of_trade(t)) is not None and pred(sp)]
        r_vals = [t["r_multiple"] for t in bucket if "r_multiple" in t]
        out[label] = {
            "count": len(bucket),
            "expectancy_r": round(sum(r_vals) / len(r_vals), 4) if r_vals else 0.0,
        }
    return out


def _compute_diagnostics(trades: List[Dict]) -> Dict:
    sl_pcts = [p for t in trades if (p := _sl_pct_of_trade(t)) is not None]
    return {
        "sl_pct_stats": _sl_pct_stats(sl_pcts),
        "cost_r_stats": _cost_r_stats(trades),
        "fill_bar_sl_exits": _fill_bar_sl_exits(trades),
        "r_by_sl_bucket": _r_by_sl_bucket(trades),
    }


# ─────────────────────────────────────────────────────────────────────────────
# vbt_legacy — original close-only vectorbt simulation, unchanged
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(value, default: float = 0.0) -> float:
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _simulate_portfolio(
    candles_15m: List[Dict],
    signals: List[Dict],
    init_cash: float,
    fees_pct: float,
    slippage_pct: float,
) -> Dict:
    n = len(candles_15m)
    close = np.array([c["close"] for c in candles_15m], dtype=float)
    index = pd.to_datetime([c["timestamp"] for c in candles_15m])

    long_entries = np.zeros(n, dtype=bool)
    short_entries = np.zeros(n, dtype=bool)
    sl_stop = np.full(n, np.nan)
    tp_stop = np.full(n, np.nan)

    for sig in signals:
        i = sig["bar_index"]
        if i >= n or close[i] <= 0:
            continue
        entry = sig["entry_price"]
        sl_stop[i] = abs(entry - sig["stop_loss"]) / entry
        tp_stop[i] = abs(sig["tp1"] - entry) / entry
        if sig["direction"] == "LONG":
            long_entries[i] = True
        else:
            short_entries[i] = True

    close_s = pd.Series(close, index=index)
    empty_bool = pd.Series(False, index=index)

    portfolio = vbt.Portfolio.from_signals(
        close=close_s,
        entries=pd.Series(long_entries, index=index),
        exits=empty_bool,
        short_entries=pd.Series(short_entries, index=index),
        short_exits=empty_bool,
        sl_stop=sl_stop,
        tp_stop=tp_stop,
        fees=fees_pct,
        slippage=slippage_pct,
        init_cash=init_cash,
        freq="15min",
    )

    trades_df = portfolio.trades.records_readable
    has_trades = len(trades_df) > 0

    equity = portfolio.value()
    equity_curve = [{"timestamp": str(ts), "equity": round(_safe_float(v, init_cash), 2)} for ts, v in equity.items()]
    if len(equity_curve) > 2000:
        step = max(1, len(equity_curve) // 2000)
        equity_curve = equity_curve[::step]

    trade_list = [
        {
            "entry_time": str(row.get("Entry Timestamp")),
            "exit_time": str(row.get("Exit Timestamp")),
            "direction": "LONG" if row.get("Direction") == "Long" else "SHORT",
            "entry_price": round(_safe_float(row.get("Avg Entry Price")), 8),
            "exit_price": round(_safe_float(row.get("Avg Exit Price")), 8),
            "pnl": round(_safe_float(row.get("PnL")), 4),
            "return_pct": round(_safe_float(row.get("Return")) * 100, 3),
        }
        for _, row in trades_df.iterrows()
    ]

    profit_factor_raw = portfolio.trades.profit_factor() if has_trades else 0.0
    try:
        profit_factor = round(float(profit_factor_raw), 3) if math.isfinite(float(profit_factor_raw)) else None
    except (TypeError, ValueError):
        profit_factor = None

    return {
        "final_equity": round(_safe_float(equity.iloc[-1], init_cash), 2) if len(equity) else init_cash,
        "total_return_pct": round(_safe_float(portfolio.total_return()) * 100, 2),
        "win_rate_pct": round(_safe_float(portfolio.trades.win_rate()) * 100, 2) if has_trades else 0.0,
        "sharpe_ratio": round(_safe_float(portfolio.sharpe_ratio()), 3) if has_trades else 0.0,
        "max_drawdown_pct": round(abs(_safe_float(portfolio.max_drawdown())) * 100, 2),
        "profit_factor": profit_factor,
        "total_trades": len(trade_list),
        "trades": trade_list,
        "equity_curve": equity_curve,
    }


def _empty_result(
    symbol: str, since: datetime, until: datetime, bars: int, sampled: List[Dict],
    sim_mode: str = "event", sim_config: Optional[SimConfig] = None,
    decision_schedule: str = "every_bar", decision_bars: int = 0,
    signal_config: Optional[SignalConfig] = None,
    lookback_start: Optional[str] = None, lookback_complete: bool = True,
    lookback_by_tf: Optional[Dict] = None, smc_config: Optional[SmcConfig] = None,
) -> Dict:
    return {
        "symbol": symbol, "since": since.isoformat(), "until": until.isoformat(),
        "lookback_start": lookback_start if lookback_start is not None else since.isoformat(),
        "lookback_complete": lookback_complete,
        "lookback_by_tf": lookback_by_tf or {},
        "smc_config": _smc_config_as_dict(smc_config),
        "bars_analyzed": bars, "signals_generated": 0,
        "final_equity": None, "total_return_pct": 0.0, "win_rate_pct": 0.0,
        "sharpe_ratio": 0.0, "max_drawdown_pct": 0.0, "profit_factor": 0.0,
        "expectancy_r": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0,
        "total_trades": 0, "trades": [], "equity_curve": [], "claude_sample": sampled,
        "orders": None,
        "sim_mode": sim_mode,
        "sim_config": _sim_config_as_dict(sim_config) if sim_mode == "event" else None,
        "decision_schedule": decision_schedule,
        "decision_bars": decision_bars,
        "signal_config": _signal_config_as_dict(signal_config or SignalConfig()),
        "claude_vs_proxy": _compute_claude_vs_proxy(sampled),
        **_NEUTRAL_DIAGNOSTICS,
        "note": "No qualifying structural setups found in this range.",
    }
