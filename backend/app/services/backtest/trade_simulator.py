"""
Event-driven backtest trade simulator — Phase 6 (backtest fidelity).

Models the live execution flow (executor.py + paper_wallet.py) instead of
vectorbt's close-only fills: a LIMIT entry that has to actually be touched
by price, an SL and split TP1/TP2 checked against bar wicks (not just
closes), SL moved to breakeven after TP1, and the same order-cancellation
edges a live system hits (superseded by a new signal, the setup's bias
flipping before fill, price running to TP1 before the entry even filled,
or the order just going stale).

Pure Python, deterministic, no I/O — takes the candles/signals/bias arrays
the caller already has and returns a plain dict.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional

_SIZING_MODES = ("risk_pct", "fixed_risk_usdt", "fixed_margin_usdt")

# Annualization factor for a 15m-bar Sharpe ratio: 96 bars/day * 365 days/yr.
_BARS_PER_YEAR = 365 * 96


@dataclass(frozen=True)
class SimConfig:
    init_cash: float = 1000.0
    # MEXC futures base tier: 0% maker / 0.02% taker. Limit entries fill maker;
    # SL/TP1/TP2/EOD exits are market-type and fill taker.
    maker_fee_pct: float = 0.0
    taker_fee_pct: float = 0.0002
    slippage_pct: float = 0.0005      # applied to market-type fills only (stop/TP market, EOD close)
    order_ttl_bars: int = 16          # safety net: unfilled limit cancelled after N 15m bars (4h)
    cancel_on_tp1_before_fill: bool = True
    cancel_on_bias_flip: bool = True
    tp1_fraction: float = 0.5
    move_sl_to_be_after_tp1: bool = True
    sizing_mode: str = "risk_pct"     # "risk_pct" | "fixed_risk_usdt" | "fixed_margin_usdt"
    risk_pct: float = 1.0             # % of current equity risked per trade (live default)
    fixed_risk_usdt: float = 3.0      # USDT lost if SL hits
    fixed_margin_usdt: float = 3.0    # USDT margin per trade (risk then varies with SL distance)
    leverage: int = 10
    # A setup (direction, entry, SL, TP1, TP2) is placed at most once by
    # default — a later identical signal is blocked (orders.setup_reused_blocked)
    # regardless of what happened to the earlier order (filled and closed,
    # or cancelled for any reason). True restores the old always-re-place behavior.
    allow_setup_reentry: bool = False

    def __post_init__(self):
        if self.sizing_mode not in _SIZING_MODES:
            raise ValueError(f"sizing_mode must be one of {_SIZING_MODES}, got {self.sizing_mode!r}")
        for field_name in ("init_cash", "leverage"):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if not (0 < self.tp1_fraction <= 1):
            raise ValueError("tp1_fraction must be in (0, 1]")
        if self.order_ttl_bars <= 0:
            raise ValueError("order_ttl_bars must be positive")
        if self.maker_fee_pct < 0 or self.taker_fee_pct < 0 or self.slippage_pct < 0:
            raise ValueError("maker_fee_pct/taker_fee_pct/slippage_pct must be >= 0")
        if self.sizing_mode == "risk_pct" and self.risk_pct <= 0:
            raise ValueError("risk_pct must be positive for sizing_mode='risk_pct'")
        if self.sizing_mode == "fixed_risk_usdt" and self.fixed_risk_usdt <= 0:
            raise ValueError("fixed_risk_usdt must be positive for sizing_mode='fixed_risk_usdt'")
        if self.sizing_mode == "fixed_margin_usdt" and self.fixed_margin_usdt <= 0:
            raise ValueError("fixed_margin_usdt must be positive for sizing_mode='fixed_margin_usdt'")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Sizing — mirrors executor.calculate_position_size(), capped at equity
# ─────────────────────────────────────────────────────────────────────────────

def _size_position(equity: float, fill_price: float, stop_loss: float, config: SimConfig):
    sl_dist = abs(fill_price - stop_loss)
    sl_dist_pct = sl_dist / fill_price if fill_price > 0 else 0.01
    if sl_dist_pct <= 0:
        sl_dist_pct = 0.01

    if config.sizing_mode == "risk_pct":
        risk_usdt = equity * (config.risk_pct / 100.0)
        margin = risk_usdt / (sl_dist_pct * config.leverage)
    elif config.sizing_mode == "fixed_risk_usdt":
        margin = config.fixed_risk_usdt / (sl_dist_pct * config.leverage)
    else:  # fixed_margin_usdt
        margin = config.fixed_margin_usdt

    margin_capped = False
    if margin > equity:
        margin = equity
        margin_capped = True
    margin = max(margin, 0.0)

    qty = (margin * config.leverage) / fill_price if fill_price > 0 else 0.0
    return margin, qty, margin_capped


# ─────────────────────────────────────────────────────────────────────────────
# Fill mechanics
# ─────────────────────────────────────────────────────────────────────────────

def _try_limit_fill(order: Dict, bar: Dict) -> Optional[float]:
    """LONG fills if low <= entry, SHORT fills if high >= entry. No slippage."""
    entry = order["entry_price"]
    if order["direction"] == "LONG":
        if bar["low"] <= entry:
            return min(bar["open"], entry)
    else:
        if bar["high"] >= entry:
            return max(bar["open"], entry)
    return None


def _reached_tp1(order: Dict, bar: Dict) -> bool:
    if order["direction"] == "LONG":
        return bar["high"] >= order["tp1"]
    return bar["low"] <= order["tp1"]


def _closing_fill_price(raw_price: float, direction: str, slippage_pct: float) -> float:
    """Market-type closing fill: adverse slippage (sell => worse/lower,
    buy-to-cover => worse/higher)."""
    if direction == "LONG":
        return raw_price * (1 - slippage_pct)
    return raw_price * (1 + slippage_pct)


def _is_opposite_bias(bias: Optional[str], direction: str) -> bool:
    if bias is None:
        return False
    if direction == "LONG":
        return bias == "bearish"
    return bias == "bullish"


def _same_setup(a: Dict, b: Dict) -> bool:
    """Same direction and same entry/SL/TP1/TP2 — the setup key shared by
    duplicate-signal detection (against the currently pending order) and
    setup-reuse blocking (against every setup ever placed)."""
    if a["direction"] != b["direction"]:
        return False
    return (
        math.isclose(a["entry_price"], b["entry_price"], rel_tol=1e-9)
        and math.isclose(a["stop_loss"], b["stop_loss"], rel_tol=1e-9)
        and math.isclose(a["tp1"], b["tp1"], rel_tol=1e-9)
        and math.isclose(a["tp2"], b["tp2"], rel_tol=1e-9)
    )


def _is_duplicate_signal(order: Dict, sig: Dict) -> bool:
    """Same setup as the pending order — the rule-based signal re-firing on
    every bar a setup persists, not a genuinely new setup."""
    return _same_setup(order, sig)


def _setup_key(sig: Dict) -> tuple:
    """Hashable setup identity — rounds to 8 decimals rather than the
    isclose(rel_tol=1e-9) comparison _same_setup uses, but at the price
    magnitudes this backtester deals with the two agree in practice, and a
    set lookup replaces an O(n) scan over every setup ever placed."""
    return (
        sig["direction"],
        round(sig["entry_price"], 8),
        round(sig["stop_loss"], 8),
        round(sig["tp1"], 8),
        round(sig["tp2"], 8),
    )


def _matches_any_setup(sig: Dict, placed_setup_keys: set) -> bool:
    """True if `sig` is the same setup as any setup ever placed (filled,
    cancelled, or still pending/open) — used to block re-placing it."""
    return _setup_key(sig) in placed_setup_keys


# ─────────────────────────────────────────────────────────────────────────────
# Position event checks — priority SL > TP1 > TP2, one event per bar
# ─────────────────────────────────────────────────────────────────────────────

def _check_sl(position: Dict, bar: Dict):
    """Returns (hit: bool, raw_exit_price)."""
    sl = position["stop_loss"]
    if position["direction"] == "LONG":
        if bar["low"] <= sl:
            raw = bar["open"] if bar["open"] <= sl else sl
            return True, raw
    else:
        if bar["high"] >= sl:
            raw = bar["open"] if bar["open"] >= sl else sl
            return True, raw
    return False, None


def _check_tp(position: Dict, level_key: str, bar: Dict):
    level = position[level_key]
    if position["direction"] == "LONG":
        return bar["high"] >= level
    return bar["low"] <= level


# ─────────────────────────────────────────────────────────────────────────────
# Main simulation
# ─────────────────────────────────────────────────────────────────────────────

def simulate(
    candles_15m: List[Dict],
    signals: List[Dict],
    bias_by_bar: Dict[int, str],
    config: Optional[SimConfig] = None,
) -> Dict:
    """
    Event-driven, live-like backtest simulation.

    Args:
        candles_15m: full 15m OHLCV series (chronological).
        signals: TRADE-decision signals, each
            {bar_index, timestamp, direction, entry_price, stop_loss, tp1, tp2, confidence}.
        bias_by_bar: bar_index -> "bullish" | "bearish" | "neutral" for every
            replayed bar (used for bias-flip order cancellation).
        config: SimConfig (defaults applied if omitted).

    Returns: dict — see module docstring / engine.py for the full result shape.
    """
    config = config or SimConfig()
    signals_by_bar = {s["bar_index"]: s for s in signals}

    equity = config.init_cash
    state_kind: Optional[str] = None   # None | "PENDING" | "POSITION"
    order: Optional[Dict] = None
    position: Optional[Dict] = None

    trades: List[Dict] = []
    equity_curve_raw: List[float] = []
    order_counts = {
        "placed": 0, "filled": 0,
        "cancelled": {"replaced": 0, "bias_flip": 0, "tp1_before_fill": 0, "ttl": 0},
        "ignored_in_position": 0,
        "duplicate_signals": 0,
        "setup_reused_blocked": 0,
    }
    # Keys of every setup ever placed (filled-and-closed or cancelled for any
    # reason) — a later identical signal is blocked unless allow_setup_reentry.
    placed_setup_keys: set = set()

    def _new_order(sig: Dict, placed_bar: int) -> Dict:
        return {
            "signal_time": sig["timestamp"],
            "placed_bar": placed_bar,
            "active_from": placed_bar + 1,
            "direction": sig["direction"],
            "entry_price": sig["entry_price"],
            "stop_loss": sig["stop_loss"],
            "tp1": sig["tp1"],
            "tp2": sig["tp2"],
        }

    def _open_position(ord_: Dict, fill_price: float, bar_idx: int, bar: Dict):
        # Sized on the ORDER price (what live knows at placement time via
        # calculate_position_size), not the actual fill price — a gap fill
        # must not silently change position size. PnL still uses fill_price.
        margin, qty, margin_capped = _size_position(equity, ord_["entry_price"], ord_["stop_loss"], config)
        entry_fee = config.maker_fee_pct * qty * fill_price  # limit entry fills maker
        return {
            "signal_time": ord_["signal_time"],
            "entry_time": bar["timestamp"],
            "fill_bar": bar_idx,
            "direction": ord_["direction"],
            "entry_price": fill_price,
            "stop_loss": ord_["stop_loss"],
            "original_stop_loss": ord_["stop_loss"],
            "tp1": ord_["tp1"],
            "tp2": ord_["tp2"],
            "margin": margin,
            "qty_total": qty,
            "qty_remaining": qty,
            "margin_capped": margin_capped,
            "tp1_hit": False,
            "realized_pnl": -entry_fee,   # entry fee charged immediately
            "fees_total": entry_fee,
            "slippage_cost": 0.0,          # no slippage on the (limit) entry
            "exit_legs": [],  # list of (qty, price, time)
        }

    def _leg_pnl(pos: Dict, qty_leg: float, exit_price: float) -> float:
        if pos["direction"] == "LONG":
            return qty_leg * (exit_price - pos["entry_price"])
        return qty_leg * (pos["entry_price"] - exit_price)

    def _apply_exit_leg(pos: Dict, qty_leg: float, raw_price: float, bar: Dict) -> None:
        exit_price = _closing_fill_price(raw_price, pos["direction"], config.slippage_pct)
        fee = config.taker_fee_pct * qty_leg * exit_price  # SL/TP1/TP2/EOD are market-type, taker
        slip_cost = qty_leg * abs(exit_price - raw_price)
        gross = _leg_pnl(pos, qty_leg, exit_price)
        pos["realized_pnl"] += gross - fee
        pos["fees_total"] += fee
        pos["slippage_cost"] += slip_cost
        pos["qty_remaining"] -= qty_leg
        pos["exit_legs"].append((qty_leg, exit_price, bar["timestamp"]))

    def _finalize_trade(pos: Dict, outcome: str) -> Dict:
        nonlocal equity
        equity += pos["realized_pnl"]
        total_qty = sum(q for q, _, _ in pos["exit_legs"])
        exit_price = (
            sum(q * p for q, p, _ in pos["exit_legs"]) / total_qty if total_qty > 0 else pos["entry_price"]
        )
        exit_time = pos["exit_legs"][-1][2] if pos["exit_legs"] else pos["entry_time"]
        initial_risk_usdt = pos["qty_total"] * abs(pos["entry_price"] - pos["original_stop_loss"])
        r_multiple = (pos["realized_pnl"] / initial_risk_usdt) if initial_risk_usdt > 0 else 0.0
        return_pct = (pos["realized_pnl"] / pos["margin"] * 100.0) if pos["margin"] > 0 else 0.0
        return {
            "signal_time": pos["signal_time"],
            "entry_time": pos["entry_time"],
            "exit_time": exit_time,
            "direction": pos["direction"],
            "entry_price": round(pos["entry_price"], 8),
            "exit_price": round(exit_price, 8),
            "stop_loss": round(pos["original_stop_loss"], 8),
            "tp1": round(pos["tp1"], 8),
            "tp2": round(pos["tp2"], 8),
            "outcome": outcome,
            "r_multiple": round(_safe_float(r_multiple), 4),
            "pnl": round(_safe_float(pos["realized_pnl"]), 4),
            "return_pct": round(_safe_float(return_pct), 3),
            "margin": round(pos["margin"], 4),
            "qty": round(pos["qty_total"], 6),
            "fees": round(pos["fees_total"], 4),
            "slippage_cost": round(pos["slippage_cost"], 4),
            "margin_capped": pos["margin_capped"],
        }

    n = len(candles_15m)
    for j in range(n):
        bar = candles_15m[j]
        sig_here = signals_by_bar.get(j)
        consumed_signal = False

        if state_kind == "PENDING":
            fill_price = _try_limit_fill(order, bar)
            if fill_price is not None:
                position = _open_position(order, fill_price, j, bar)
                order_counts["filled"] += 1
                state_kind = "POSITION"
                order = None

                sl_hit, raw_exit = _check_sl(position, bar)
                if sl_hit:
                    _apply_exit_leg(position, position["qty_remaining"], raw_exit, bar)
                    trades.append(_finalize_trade(position, "sl"))
                    state_kind = None
                    position = None
            else:
                is_dup = sig_here is not None and _is_duplicate_signal(order, sig_here)
                if is_dup:
                    order_counts["duplicate_signals"] += 1
                    consumed_signal = True

                is_reuse_blocked = (
                    not config.allow_setup_reentry
                    and sig_here is not None and not is_dup
                    and _matches_any_setup(sig_here, placed_setup_keys)
                )
                if is_reuse_blocked:
                    order_counts["setup_reused_blocked"] += 1
                    consumed_signal = True

                if config.cancel_on_tp1_before_fill and _reached_tp1(order, bar):
                    order_counts["cancelled"]["tp1_before_fill"] += 1
                    state_kind = None
                    order = None
                elif sig_here is not None and not is_dup and not is_reuse_blocked:
                    order_counts["cancelled"]["replaced"] += 1
                    order = _new_order(sig_here, placed_bar=j)
                    order_counts["placed"] += 1
                    placed_setup_keys.add(_setup_key(order))
                    consumed_signal = True
                elif config.cancel_on_bias_flip and _is_opposite_bias(bias_by_bar.get(j), order["direction"]):
                    order_counts["cancelled"]["bias_flip"] += 1
                    state_kind = None
                    order = None
                elif (j - order["active_from"] + 1) >= config.order_ttl_bars:
                    order_counts["cancelled"]["ttl"] += 1
                    state_kind = None
                    order = None

        if state_kind == "POSITION":
            if sig_here is not None and not consumed_signal:
                order_counts["ignored_in_position"] += 1
                consumed_signal = True

            if position["fill_bar"] != j:
                if not position["tp1_hit"]:
                    sl_hit, raw_exit = _check_sl(position, bar)
                    if sl_hit:
                        _apply_exit_leg(position, position["qty_remaining"], raw_exit, bar)
                        trades.append(_finalize_trade(position, "sl"))
                        state_kind = None
                        position = None
                    elif _check_tp(position, "tp1", bar):
                        if math.isclose(position["tp2"], position["tp1"], rel_tol=1e-9):
                            # Live places TP1 and TP2 as two take_profit_market
                            # orders at the same price — they trigger together,
                            # so the whole position closes on this bar.
                            _apply_exit_leg(position, position["qty_remaining"], position["tp2"], bar)
                            trades.append(_finalize_trade(position, "tp2"))
                            state_kind = None
                            position = None
                        else:
                            tp1_qty = position["qty_total"] * config.tp1_fraction
                            tp1_qty = min(tp1_qty, position["qty_remaining"])
                            _apply_exit_leg(position, tp1_qty, position["tp1"], bar)
                            position["tp1_hit"] = True
                            if config.move_sl_to_be_after_tp1:
                                position["stop_loss"] = position["entry_price"]
                            if position["qty_remaining"] <= 1e-12:
                                trades.append(_finalize_trade(position, "tp2"))
                                state_kind = None
                                position = None
                else:
                    sl_hit, raw_exit = _check_sl(position, bar)
                    if sl_hit:
                        _apply_exit_leg(position, position["qty_remaining"], raw_exit, bar)
                        trades.append(_finalize_trade(position, "tp1_be"))
                        state_kind = None
                        position = None
                    elif _check_tp(position, "tp2", bar):
                        _apply_exit_leg(position, position["qty_remaining"], position["tp2"], bar)
                        trades.append(_finalize_trade(position, "tp2"))
                        state_kind = None
                        position = None

        if state_kind is None and sig_here is not None and not consumed_signal:
            if not config.allow_setup_reentry and _matches_any_setup(sig_here, placed_setup_keys):
                order_counts["setup_reused_blocked"] += 1
            else:
                order = _new_order(sig_here, placed_bar=j)
                order_counts["placed"] += 1
                placed_setup_keys.add(_setup_key(order))
                state_kind = "PENDING"

        if state_kind == "POSITION":
            unrealized = _leg_pnl(position, position["qty_remaining"], bar["close"])
            equity_curve_raw.append(equity + position["realized_pnl"] + unrealized)
        else:
            equity_curve_raw.append(equity)

    if state_kind == "POSITION":
        last_bar = candles_15m[-1]
        outcome = "tp1_eod" if position["tp1_hit"] else "eod"
        _apply_exit_leg(position, position["qty_remaining"], last_bar["close"], last_bar)
        trades.append(_finalize_trade(position, outcome))
        equity_curve_raw[-1] = equity

    return _build_result(candles_15m, trades, equity_curve_raw, order_counts, config)


# ─────────────────────────────────────────────────────────────────────────────
# Result assembly — metrics, downsampled equity curve, JSON-safe throughout
# ─────────────────────────────────────────────────────────────────────────────

def _build_result(candles_15m, trades, equity_curve_raw, order_counts, config: SimConfig) -> Dict:
    index_ts = [c["timestamp"] for c in candles_15m]
    final_equity = equity_curve_raw[-1] if equity_curve_raw else config.init_cash
    total_return_pct = (final_equity - config.init_cash) / config.init_cash * 100.0 if config.init_cash > 0 else 0.0

    r_multiples = [t["r_multiple"] for t in trades]
    wins = [r for r in r_multiples if r > 0]
    losses = [r for r in r_multiples if r <= 0]
    win_rate_pct = (len(wins) / len(r_multiples) * 100.0) if r_multiples else 0.0
    expectancy_r = (sum(r_multiples) / len(r_multiples)) if r_multiples else 0.0
    avg_win_r = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss_r = (sum(losses) / len(losses)) if losses else 0.0

    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    profit_factor = round(gross_win / gross_loss, 3) if gross_loss > 0 else None

    if len(equity_curve_raw) >= 2:
        rets = []
        prev = equity_curve_raw[0]
        for eq in equity_curve_raw[1:]:
            rets.append((eq - prev) / prev if prev != 0 else 0.0)
            prev = eq
        mean_ret = sum(rets) / len(rets)
        variance = sum((r - mean_ret) ** 2 for r in rets) / len(rets)
        std_ret = math.sqrt(variance)
        sharpe_ratio = (mean_ret / std_ret * math.sqrt(_BARS_PER_YEAR)) if std_ret > 0 else 0.0
    else:
        sharpe_ratio = 0.0

    peak = -math.inf
    max_dd = 0.0
    for eq in equity_curve_raw:
        peak = max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)

    equity_curve = [
        {"timestamp": ts, "equity": round(_safe_float(eq, config.init_cash), 2)}
        for ts, eq in zip(index_ts, equity_curve_raw)
    ]
    if len(equity_curve) > 2000:
        step = max(1, len(equity_curve) // 2000)
        equity_curve = equity_curve[::step]

    fill_rate_pct = (
        order_counts["filled"] / order_counts["placed"] * 100.0 if order_counts["placed"] else 0.0
    )
    margin_capped_trades = sum(1 for t in trades if t["margin_capped"])

    return {
        "final_equity": round(_safe_float(final_equity, config.init_cash), 2),
        "total_return_pct": round(_safe_float(total_return_pct), 2),
        "win_rate_pct": round(_safe_float(win_rate_pct), 2),
        "expectancy_r": round(_safe_float(expectancy_r), 4),
        "avg_win_r": round(_safe_float(avg_win_r), 4),
        "avg_loss_r": round(_safe_float(avg_loss_r), 4),
        "profit_factor": profit_factor,
        "sharpe_ratio": round(_safe_float(sharpe_ratio), 3),
        "max_drawdown_pct": round(_safe_float(max_dd * 100.0), 2),
        "total_trades": len(trades),
        "trades": trades,
        "equity_curve": equity_curve,
        "orders": {
            "placed": order_counts["placed"],
            "filled": order_counts["filled"],
            "fill_rate_pct": round(_safe_float(fill_rate_pct), 2),
            "cancelled": dict(order_counts["cancelled"]),
            "ignored_in_position": order_counts["ignored_in_position"],
            "duplicate_signals": order_counts["duplicate_signals"],
            "margin_capped_trades": margin_capped_trades,
            "setup_reused_blocked": order_counts["setup_reused_blocked"],
        },
    }
