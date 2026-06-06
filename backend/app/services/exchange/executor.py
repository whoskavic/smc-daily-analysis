"""
Unified Trade Executor — single interface for all exchanges + paper mode.

Flow:
  execute_signal(signal, exchange_id)
    ├── PAPER mode  → paper_wallet.paper_open_position()
    └── LIVE mode   → _live_execute() via ccxt
         ├── set leverage
         ├── entry order (LIMIT or MARKET)
         ├── SL order (stop_market, full size, reduce_only)
         ├── TP1 order (take_profit_market, 50% size, reduce_only)
         └── TP2 order (take_profit_market, 50% size, reduce_only)

All exchange quirks (Binance algo orders, OKX params, Bybit unified)
are handled here so the caller never has to care.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_trade_mode() -> str:
    try:
        from app.config import settings
        return getattr(settings, "trade_mode", "paper").lower()
    except Exception:
        return "paper"


def _get_max_positions() -> int:
    try:
        from app.config import settings
        return getattr(settings, "max_concurrent_positions", 3)
    except Exception:
        return 3


def _get_risk_pct() -> float:
    """Fraction of balance to risk per trade (e.g. 0.01 = 1%)."""
    try:
        from app.config import settings
        return getattr(settings, "risk_per_trade_pct", 1.0) / 100.0
    except Exception:
        return 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Position sizing
# ─────────────────────────────────────────────────────────────────────────────

def calculate_position_size(
    balance_usdt: float,
    entry_price: float,
    stop_loss: float,
    risk_pct: float,
    leverage: int,
    min_notional: float = 1.0,
) -> tuple[float, float]:
    """
    Risk-based position sizing.

    risk_usdt = balance * risk_pct
    stop_distance_pct = abs(entry - sl) / entry
    margin_required = risk_usdt / (stop_distance_pct * leverage)

    Returns: (margin_usdt, quantity)
    """
    risk_usdt = balance_usdt * risk_pct
    sl_dist = abs(entry_price - stop_loss)
    sl_dist_pct = sl_dist / entry_price if entry_price > 0 else 0.01

    if sl_dist_pct == 0:
        sl_dist_pct = 0.01  # 1% fallback

    # Margin needed to risk exactly risk_usdt
    margin = risk_usdt / (sl_dist_pct * leverage)

    # Ensure minimum notional is met
    if margin * leverage < min_notional:
        margin = min_notional / leverage

    quantity = (margin * leverage) / entry_price
    return round(margin, 4), round(quantity, 6)


# ─────────────────────────────────────────────────────────────────────────────
# Live execution helpers per exchange
# ─────────────────────────────────────────────────────────────────────────────

async def _place_binance_trade(
    exchange,
    symbol: str,
    direction: str,
    size: float,
    entry_price: Optional[float],
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_size: float,
    tp2_size: float,
) -> dict:
    """Binance USDT-M futures: entry LIMIT/MARKET + algo SL + algo TP1 + algo TP2."""
    entry_side = "buy" if direction == "LONG" else "sell"
    close_side = "sell" if direction == "LONG" else "buy"

    # 1. Entry
    if entry_price:
        entry_order = await exchange.create_order(
            symbol, "limit", entry_side, size, entry_price,
            {"timeInForce": "GTC"}
        )
    else:
        entry_order = await exchange.create_order(symbol, "market", entry_side, size)

    # 2. SL — full size, reduce only
    sl_order = await exchange.create_order(
        symbol, "stop_market", close_side, size, None,
        {"stopPrice": stop_loss, "reduceOnly": True, "workingType": "CONTRACT_PRICE"}
    )

    # 3. TP1 — tp1_size, reduce only
    tp1_order = await exchange.create_order(
        symbol, "take_profit_market", close_side, tp1_size, None,
        {"stopPrice": tp1, "reduceOnly": True, "workingType": "CONTRACT_PRICE"}
    )

    # 4. TP2 — tp2_size, reduce only
    tp2_order = await exchange.create_order(
        symbol, "take_profit_market", close_side, tp2_size, None,
        {"stopPrice": tp2, "reduceOnly": True, "workingType": "CONTRACT_PRICE"}
    )

    return {
        "entry": entry_order,
        "sl": sl_order,
        "tp1": tp1_order,
        "tp2": tp2_order,
    }


async def _place_bybit_trade(
    exchange,
    symbol: str,
    direction: str,
    size: float,
    entry_price: Optional[float],
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_size: float,
    tp2_size: float,
) -> dict:
    """Bybit: entry with native TP/SL in one order, plus separate TP2 reduce-only."""
    entry_side = "buy" if direction == "LONG" else "sell"
    close_side = "sell" if direction == "LONG" else "buy"

    order_type = "limit" if entry_price else "market"
    params = {
        "stopLoss": str(stop_loss),
        "takeProfit": str(tp1),
        "tpTriggerBy": "MarkPrice",
        "slTriggerBy": "MarkPrice",
        "tpslMode": "Partial",  # allows partial TP
        "tpSize": str(tp1_size),
        "slSize": str(size),     # SL always full
        "reduceOnly": False,
        "timeInForce": "GTC",
    }

    entry_order = await exchange.create_order(
        symbol, order_type, entry_side, size,
        entry_price if entry_price else None, params
    )

    # TP2 as separate reduce-only limit order
    tp2_order = await exchange.create_order(
        symbol, "take_profit_market", close_side, tp2_size, None,
        {"triggerPrice": tp2, "reduceOnly": True, "tpTriggerBy": "MarkPrice"}
    )

    return {
        "entry": entry_order,
        "sl": {"embedded": True, "stop_loss": stop_loss},
        "tp1": {"embedded": True, "tp1": tp1},
        "tp2": tp2_order,
    }


async def _place_okx_trade(
    exchange,
    symbol: str,
    direction: str,
    size: float,
    entry_price: Optional[float],
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_size: float,
    tp2_size: float,
) -> dict:
    """OKX: entry + algo SL/TP orders."""
    entry_side = "buy" if direction == "LONG" else "sell"
    close_side = "sell" if direction == "LONG" else "buy"
    td_mode = "cross"  # cross margin — change to "isolated" if preferred

    order_type = "limit" if entry_price else "market"
    entry_order = await exchange.create_order(
        symbol, order_type, entry_side, size,
        entry_price if entry_price else None,
        {"tdMode": td_mode, "posSide": direction.lower(), "timeInForce": "gtc"}
    )

    sl_order = await exchange.create_order(
        symbol, "stop", close_side, size, None,
        {
            "tdMode": td_mode, "posSide": direction.lower(),
            "slTriggerPx": str(stop_loss), "slOrdPx": "-1",  # market fill
            "reduceOnly": True,
        }
    )

    tp1_order = await exchange.create_order(
        symbol, "stop", close_side, tp1_size, None,
        {
            "tdMode": td_mode, "posSide": direction.lower(),
            "tpTriggerPx": str(tp1), "tpOrdPx": "-1",
            "reduceOnly": True,
        }
    )

    tp2_order = await exchange.create_order(
        symbol, "stop", close_side, tp2_size, None,
        {
            "tdMode": td_mode, "posSide": direction.lower(),
            "tpTriggerPx": str(tp2), "tpOrdPx": "-1",
            "reduceOnly": True,
        }
    )

    return {"entry": entry_order, "sl": sl_order, "tp1": tp1_order, "tp2": tp2_order}


async def _place_mexc_trade(
    exchange,
    symbol: str,
    direction: str,
    size: float,
    entry_price: Optional[float],
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_size: float,
    tp2_size: float,
) -> dict:
    """MEXC futures: similar to Binance layout."""
    entry_side = "buy" if direction == "LONG" else "sell"
    close_side = "sell" if direction == "LONG" else "buy"

    order_type = "limit" if entry_price else "market"
    entry_order = await exchange.create_order(
        symbol, order_type, entry_side, size,
        entry_price if entry_price else None
    )

    sl_order = await exchange.create_order(
        symbol, "stop_market", close_side, size, None,
        {"stopPrice": stop_loss, "reduceOnly": True}
    )

    tp1_order = await exchange.create_order(
        symbol, "take_profit_market", close_side, tp1_size, None,
        {"stopPrice": tp1, "reduceOnly": True}
    )

    tp2_order = await exchange.create_order(
        symbol, "take_profit_market", close_side, tp2_size, None,
        {"stopPrice": tp2, "reduceOnly": True}
    )

    return {"entry": entry_order, "sl": sl_order, "tp1": tp1_order, "tp2": tp2_order}


# ─────────────────────────────────────────────────────────────────────────────
# Live execution dispatcher
# ─────────────────────────────────────────────────────────────────────────────

_PLACE_FN = {
    "binance": _place_binance_trade,
    "bybit": _place_bybit_trade,
    "okx": _place_okx_trade,
    "mexc": _place_mexc_trade,
}


async def _live_execute(
    exchange_id: str,
    symbol: str,
    direction: str,
    entry_price: Optional[float],
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_pct: float,
    leverage: int,
    balance_usdt: float,
) -> dict:
    """Execute a live trade on the target exchange via ccxt."""
    from app.services.exchange.factory import create_exchange, get_meta

    meta = get_meta(exchange_id)
    risk_pct = _get_risk_pct()

    ref_price = entry_price or 0.0
    if not ref_price:
        # Fetch current price for size calculation
        async with create_exchange(exchange_id) as client:
            ticker = await client.exchange.fetch_ticker(symbol)
            ref_price = float(ticker.get("last", 0))

    margin_usdt, size = calculate_position_size(
        balance_usdt, ref_price, stop_loss, risk_pct, leverage, meta.min_notional
    )

    tp1_size = round(size * tp1_pct, 6)
    tp2_size = round(size * (1 - tp1_pct), 6)

    async with create_exchange(exchange_id) as client:
        ex = client.exchange

        # Set leverage
        try:
            await ex.set_leverage(leverage, symbol)
        except Exception as e:
            logger.warning(f"[Executor] set_leverage failed for {symbol} on {exchange_id}: {e}")

        place_fn = _PLACE_FN.get(exchange_id)
        if not place_fn:
            raise ValueError(f"No placement function for exchange: {exchange_id}")

        orders = await place_fn(
            exchange=ex,
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=entry_price,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp1_size=tp1_size,
            tp2_size=tp2_size,
        )

    result = {
        "exchange": exchange_id,
        "symbol": symbol,
        "direction": direction,
        "size": size,
        "entry_price": entry_price or ref_price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp1_pct": tp1_pct,
        "leverage": leverage,
        "margin_usdt": margin_usdt,
        "notional_usdt": round(margin_usdt * leverage, 2),
        "entry_order_id": str(orders["entry"].get("id", "")),
        "sl_order_id": str(orders["sl"].get("id", "embedded")),
        "tp1_order_id": str(orders["tp1"].get("id", "embedded")),
        "tp2_order_id": str(orders["tp2"].get("id", "")),
        "mode": "live",
        "status": "executed",
    }

    logger.info(
        f"[Executor] LIVE {direction} {symbol} on {exchange_id} "
        f"| Entry={entry_price or ref_price:.4f} SL={stop_loss:.4f} "
        f"TP1={tp1:.4f} TP2={tp2:.4f} Size={size:.6f} Margin=${margin_usdt:.2f}"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def execute_signal(
    signal: dict,
    exchange_id: str,
    balance_usdt: Optional[float] = None,
) -> dict:
    """
    Main entry point. Routes to paper or live based on TRADE_MODE env.

    Args:
        signal: The execution dict from Claude's JSON output, e.g.:
            {
                "decision": "TRADE",
                "direction": "SHORT",
                "entry_type": "LIMIT",
                "entry_price": 66050,
                "stop_loss": 66580,
                "tp1": 64600,
                "tp2": 62800,
                "tp1_close_pct": 50,
                "rr_ratio": 4.2,
                "confidence": 88,
                "recommended_leverage": 5,
            }
        exchange_id: Target exchange
        balance_usdt: Override balance (if None, fetched from exchange/wallet)

    Returns:
        Trade result dict (consistent shape for both paper and live)
    """
    mode = _get_trade_mode()

    # Validate signal
    exec_data = signal if "entry_price" in signal else signal.get("execution", signal)
    direction = exec_data.get("direction", "").upper()
    if direction not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid direction: {direction}")

    decision = exec_data.get("decision", "TRADE")
    if decision != "TRADE":
        raise ValueError(f"Signal is not tradeable: decision={decision}")

    entry_price = exec_data.get("entry_price")
    stop_loss = float(exec_data["stop_loss"])
    tp1 = float(exec_data["tp1"])
    tp2 = float(exec_data["tp2"])
    tp1_pct = float(exec_data.get("tp1_close_pct", 50)) / 100.0
    leverage = int(exec_data.get("recommended_leverage", 5))
    symbol = exec_data.get("symbol") or signal.get("symbol", "BTC/USDT")

    # Guard: max concurrent positions
    max_pos = _get_max_positions()

    if mode == "paper":
        from app.services.exchange.paper_wallet import (
            paper_open_position, get_position_count, get_wallet_state, has_position
        )

        if has_position(symbol):
            raise ValueError(f"[Paper] Already have an open position for {symbol}")

        if get_position_count() >= max_pos:
            raise ValueError(
                f"[Paper] Max concurrent positions ({max_pos}) reached. "
                "No new trades until existing ones close."
            )

        wallet = get_wallet_state()
        avail = wallet["available_usdt"]
        # Risk 1% of available balance
        risk_pct = _get_risk_pct()
        sl_dist = abs((entry_price or 0) - stop_loss)
        sl_pct = sl_dist / (entry_price or stop_loss) if (entry_price or stop_loss) else 0.02
        margin = (avail * risk_pct) / (sl_pct * leverage) if sl_pct else avail * 0.05
        margin = max(margin, 5.0)  # at least $5 for paper

        result = await paper_open_position(
            symbol=symbol,
            direction=direction,
            usdt_amount=round(margin, 2),
            leverage=leverage,
            entry_price=entry_price,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp1_pct=tp1_pct,
            exchange_id=exchange_id,
        )
        result["mode"] = "paper"
        return result

    else:  # live
        # Fetch balance if not provided
        if balance_usdt is None:
            from app.services.exchange.factory import create_exchange
            try:
                async with create_exchange(exchange_id) as client:
                    bal = await client.exchange.fetch_balance()
                    balance_usdt = float(bal["USDT"]["free"])
            except Exception as e:
                logger.error(f"[Executor] Could not fetch balance: {e}")
                balance_usdt = 100.0  # safe fallback

        return await _live_execute(
            exchange_id=exchange_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp1_pct=tp1_pct,
            leverage=leverage,
            balance_usdt=balance_usdt,
        )


async def get_live_positions(exchange_id: str) -> list[dict]:
    """Fetch open positions from a live exchange."""
    from app.services.exchange.factory import create_exchange
    try:
        async with create_exchange(exchange_id) as client:
            raw = await client.exchange.fetch_positions()
            return [
                {
                    "symbol": p["symbol"],
                    "direction": "LONG" if p["side"] == "long" else "SHORT",
                    "size": p["contracts"],
                    "entry_price": p["entryPrice"],
                    "mark_price": p["markPrice"],
                    "unrealized_pnl": p["unrealizedPnl"],
                    "leverage": p["leverage"],
                    "liquidation_price": p.get("liquidationPrice"),
                    "exchange": exchange_id,
                }
                for p in raw
                if p.get("contracts", 0) and p["contracts"] != 0
            ]
    except Exception as e:
        logger.error(f"[Executor] fetch_positions failed on {exchange_id}: {e}")
        return []


async def get_account_balance(exchange_id: str) -> dict:
    """Fetch USDT balance from live exchange."""
    from app.services.exchange.factory import create_exchange
    try:
        async with create_exchange(exchange_id) as client:
            bal = await client.exchange.fetch_balance()
            usdt = bal.get("USDT", {})
            return {
                "exchange": exchange_id,
                "total": float(usdt.get("total", 0)),
                "free": float(usdt.get("free", 0)),
                "used": float(usdt.get("used", 0)),
            }
    except Exception as e:
        logger.error(f"[Executor] fetch_balance failed on {exchange_id}: {e}")
        return {"exchange": exchange_id, "total": 0, "free": 0, "used": 0}
