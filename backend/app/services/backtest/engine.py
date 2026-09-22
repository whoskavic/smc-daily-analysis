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

    candles_15m = data_loader.fetch_historical_ohlcv(symbol, "15m", since, until)
    candles_1h = data_loader.fetch_historical_ohlcv(symbol, "1h", since, until)
    candles_4h = data_loader.fetch_historical_ohlcv(symbol, "4h", since, until)
    candles_1d = data_loader.fetch_historical_ohlcv(symbol, "1d", since, until)

    if len(candles_15m) < MIN_BARS_REQUIRED:
        raise ValueError(
            f"Not enough {symbol} 15m history in range to backtest "
            f"({len(candles_15m)} bars, need >= {MIN_BARS_REQUIRED})"
        )

    from app.config import settings

    decision_indices = _decision_bar_indices(
        candles_15m, smc_replay.DEFAULT_WARMUP_BARS, decision_schedule,
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

    for snap in smc_replay.replay(candles_15m, candles_1h, candles_4h, candles_1d):
        bias_by_bar[snap["bar_index"]] = signal_simulator.primary_bias(
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
            sampled.append(_run_claude_sample(symbol, snap, sig))

        decision_bar_counter += 1

        if not has_setup or sig["decision"] != "TRADE":
            continue

        signals.append({"bar_index": snap["bar_index"], "timestamp": snap["timestamp"], **sig})

    if not signals:
        empty_cfg = sim_config or (_default_sim_config(init_cash, slippage_pct) if sim_mode == "event" else None)
        return _empty_result(
            symbol, since, until, len(candles_15m), sampled, sim_mode, empty_cfg,
            decision_schedule, total_decision_bars, signal_config,
        )

    if sim_mode == "vbt_legacy":
        stats = _simulate_portfolio(candles_15m, signals, init_cash, fees_pct, slippage_pct)
        stats.setdefault("expectancy_r", 0.0)
        stats.setdefault("avg_win_r", 0.0)
        stats.setdefault("avg_loss_r", 0.0)
        stats.setdefault("orders", None)
        stats.update(_NEUTRAL_DIAGNOSTICS)
    else:
        cfg = sim_config or _default_sim_config(init_cash, slippage_pct)
        stats = trade_simulator.simulate(candles_15m, signals, bias_by_bar, cfg)
        sim_config = cfg
        stats.update(_compute_diagnostics(stats["trades"]))

    stats.update({
        "symbol": symbol,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "bars_analyzed": len(candles_15m),
        "signals_generated": len(signals),
        "claude_sample": sampled,
        "sim_mode": sim_mode,
        "sim_config": _sim_config_as_dict(sim_config) if sim_mode == "event" else None,
        "decision_schedule": decision_schedule,
        "decision_bars": total_decision_bars,
        "signal_config": _signal_config_as_dict(signal_config),
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


def _run_claude_sample(symbol: str, snap: Dict, rule_sig: Dict) -> Dict:
    """
    Cross-check the rule-based proxy against a real Claude call on the same
    historical snapshot. The snapshot mirrors live's
    snapshot_builder.build_enriched_snapshot() key set; fields that can't be
    reconstructed from historical OHLCV alone (funding rate, fear & greed,
    and everything in ticker but `last`) are set to None — see the PR report
    for the full list.
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
            "ticker": {
                "symbol": symbol, "last": snap["close_price"],
                "high": None, "low": None, "volume": None, "change_pct": None,
            },
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
        claude_entry = claude_exec.get("entry_price")
        claude_sl = claude_exec.get("stop_loss")
        record.update({
            "claude_decision": claude_exec.get("decision"),
            "claude_direction": claude_exec.get("direction"),
            "claude_entry": claude_entry,
            "claude_sl": claude_sl,
            "claude_tp1": claude_exec.get("tp1"),
            "claude_tp2": claude_exec.get("tp2"),
            "claude_confidence": claude_exec.get("confidence"),
            "claude_rr": claude_exec.get("rr_ratio"),
            "claude_sl_pct": _sl_pct(claude_entry, claude_sl),
            "agree_decision": rule_sig.get("decision") == claude_exec.get("decision"),
            "agree_direction": rule_sig.get("direction") == claude_exec.get("direction"),
        })
    except Exception as e:
        logger.warning(f"[Backtest] Claude sample failed at {snap['timestamp']}: {e}")
        record["error"] = str(e)
    return record


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
) -> Dict:
    return {
        "symbol": symbol, "since": since.isoformat(), "until": until.isoformat(),
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
        **_NEUTRAL_DIAGNOSTICS,
        "note": "No qualifying structural setups found in this range.",
    }
