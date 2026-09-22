"""
Backtest execution engine — Phase 4.

Orchestrates: fetch historical OHLCV (data_loader) → walk-forward SMC replay
(smc_replay, reusing smc_engine.py unchanged) → rule-based signal generation
(signal_simulator, with optional Claude-sample cross-check) → vectorbt
portfolio simulation → equity curve / win rate / Sharpe / drawdown / profit
factor.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import vectorbt as vbt

from app.services import smc_engine
from app.services.backtest import data_loader, smc_replay, signal_simulator, trade_simulator
from app.services.backtest.trade_simulator import SimConfig

logger = logging.getLogger(__name__)

DEFAULT_FEES_PCT = 0.0004       # 4bps taker per side (typical Binance USDT-M futures)
DEFAULT_SLIPPAGE_PCT = 0.0005
MIN_BARS_REQUIRED = 200


def run_backtest(
    symbol: str,
    since: datetime,
    until: Optional[datetime] = None,
    init_cash: float = 1000.0,
    fees_pct: float = DEFAULT_FEES_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    claude_sample_pct: float = 0.0,
    sim_mode: str = "event",
    sim_config: Optional[SimConfig] = None,
) -> Dict:
    """
    Full backtest run for one symbol over [since, until).

    Args:
        symbol: e.g. "BTC/USDT"
        since / until: UTC datetime range (until defaults to now)
        init_cash: virtual starting balance, USDT
        fees_pct / slippage_pct: per-side, as a fraction (0.0004 = 4bps)
        claude_sample_pct: 0-1, fraction of qualifying bars to additionally
            cross-check against a real Claude API call (0 = disabled, default)
        sim_mode: "event" (default) — event-driven trade_simulator, modeling
            live's LIMIT entry + split TP1/TP2 + breakeven SL. "vbt_legacy" —
            the original close-only vectorbt portfolio simulation, kept for
            one release to compare against.
        sim_config: SimConfig for sim_mode="event"; if omitted, one is built
            from init_cash/fees_pct/slippage_pct plus settings.risk_per_trade_pct
            and settings.max_leverage.

    Returns a dict with summary stats, trade list, equity curve, and (if
    sampled) a rule-vs-Claude decision comparison — see engine tests for the
    exact shape.
    """
    if sim_mode not in ("event", "vbt_legacy"):
        raise ValueError(f"sim_mode must be 'event' or 'vbt_legacy', got {sim_mode!r}")

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

    signals: List[Dict] = []
    sampled: List[Dict] = []
    bias_by_bar: Dict[int, str] = {}

    for snap in smc_replay.replay(candles_15m, candles_1h, candles_4h, candles_1d):
        bias_by_bar[snap["bar_index"]] = signal_simulator.primary_bias(
            snap["smc_levels"].get("confluence", {})
        )

        if not smc_engine.has_structural_setup(snap["smc_levels"]):
            continue

        sig = signal_simulator.rule_based_signal(snap["smc_levels"], snap["close_price"])

        if signal_simulator.should_sample(claude_sample_pct):
            sampled.append(_run_claude_sample(symbol, snap, sig))

        if sig["decision"] != "TRADE":
            continue

        signals.append({"bar_index": snap["bar_index"], "timestamp": snap["timestamp"], **sig})

    if not signals:
        empty_cfg = sim_config or (_default_sim_config(init_cash, fees_pct, slippage_pct) if sim_mode == "event" else None)
        return _empty_result(symbol, since, until, len(candles_15m), sampled, sim_mode, empty_cfg)

    if sim_mode == "vbt_legacy":
        stats = _simulate_portfolio(candles_15m, signals, init_cash, fees_pct, slippage_pct)
        stats.setdefault("expectancy_r", 0.0)
        stats.setdefault("avg_win_r", 0.0)
        stats.setdefault("avg_loss_r", 0.0)
        stats.setdefault("orders", None)
    else:
        cfg = sim_config or _default_sim_config(init_cash, fees_pct, slippage_pct)
        stats = trade_simulator.simulate(candles_15m, signals, bias_by_bar, cfg)
        sim_config = cfg

    stats.update({
        "symbol": symbol,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "bars_analyzed": len(candles_15m),
        "signals_generated": len(signals),
        "claude_sample": sampled,
        "sim_mode": sim_mode,
        "sim_config": _sim_config_as_dict(sim_config) if sim_mode == "event" else None,
    })
    return stats


def _default_sim_config(init_cash: float, fees_pct: float, slippage_pct: float) -> SimConfig:
    from app.config import settings
    return SimConfig(
        init_cash=init_cash,
        fees_pct=fees_pct,
        slippage_pct=slippage_pct,
        risk_pct=getattr(settings, "risk_per_trade_pct", 1.0),
        leverage=getattr(settings, "max_leverage", 10),
    )


def _sim_config_as_dict(cfg: Optional[SimConfig]) -> Optional[Dict]:
    if cfg is None:
        return None
    return {
        "init_cash": cfg.init_cash,
        "fees_pct": cfg.fees_pct,
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


def _run_claude_sample(symbol: str, snap: Dict, rule_sig: Dict) -> Dict:
    try:
        claude_exec = signal_simulator.claude_sample_signal({
            "symbol": symbol,
            "ticker": {"last": snap["close_price"]},
            "candles_1d": snap["candles_1d"],
            "candles_4h": snap["candles_4h"],
            "candles_1h": snap["candles_1h"],
            "candles_15m": snap["candles_15m"],
            "kill_zone": snap["kill_zone"],
            "smc_levels": snap["smc_levels"],
        })
        return {
            "timestamp": snap["timestamp"],
            "rule_decision": rule_sig["decision"],
            "claude_decision": claude_exec.get("decision"),
            "rule_confidence": rule_sig["confidence"],
            "claude_confidence": claude_exec.get("confidence"),
            "agree": rule_sig["decision"] == claude_exec.get("decision"),
        }
    except Exception as e:
        logger.warning(f"[Backtest] Claude sample failed at {snap['timestamp']}: {e}")
        return {"timestamp": snap["timestamp"], "error": str(e)}


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
        "note": "No qualifying structural setups found in this range.",
    }
