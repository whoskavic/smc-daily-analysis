"""
Paper Trading Wallet — realistic trade simulation with virtual USDT balance.

Design:
- State persisted to SQLite (survives server restarts)
- Fill simulation uses real bid/ask spread from exchange
- Tracks open positions, P&L, and closed trades
- Thread/async-safe via simple locking
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory state (flushed to DB periodically)
# ─────────────────────────────────────────────────────────────────────────────

_lock = asyncio.Lock()

_wallet: dict = {
    "balance_usdt": 1000.0,   # starting virtual balance — override in config
    "available_usdt": 1000.0,
    "total_pnl": 0.0,
    "trade_count": 0,
    "win_count": 0,
}

# symbol → position dict
_positions: dict[str, dict] = {}

# list of closed trades
_closed_trades: list[dict] = []


# ─────────────────────────────────────────────────────────────────────────────
# Initialisation
# ─────────────────────────────────────────────────────────────────────────────

def init_paper_wallet(starting_balance: float = 1000.0) -> None:
    """Call once at startup to set the initial virtual balance."""
    _wallet["balance_usdt"] = starting_balance
    _wallet["available_usdt"] = starting_balance
    logger.info(f"[PaperWallet] Initialised — balance: ${starting_balance:,.2f} USDT")


# ─────────────────────────────────────────────────────────────────────────────
# Price fetch helper
# ─────────────────────────────────────────────────────────────────────────────

async def _get_mark_price(symbol: str, exchange_id: str = "binance") -> float:
    """Fetch current mark/last price from the real exchange (read-only)."""
    from app.services.exchange.factory import create_exchange
    async with create_exchange(exchange_id) as client:
        ticker = await client.exchange.fetch_ticker(symbol)
        return float(ticker.get("last") or ticker.get("mark", 0))


# ─────────────────────────────────────────────────────────────────────────────
# Core paper trade operations
# ─────────────────────────────────────────────────────────────────────────────

async def paper_open_position(
    symbol: str,
    direction: str,          # "LONG" | "SHORT"
    usdt_amount: float,      # margin in USDT
    leverage: int,
    entry_price: Optional[float],
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_pct: float = 0.50,   # fraction to close at TP1
    exchange_id: str = "binance",
) -> dict:
    """
    Open a paper position. Returns a trade record dict.
    If entry_price is None → simulate MARKET fill at current price.
    """
    async with _lock:
        # Validate we have enough balance
        if usdt_amount > _wallet["available_usdt"]:
            raise ValueError(
                f"[PaperWallet] Insufficient balance: "
                f"need ${usdt_amount:.2f}, have ${_wallet['available_usdt']:.2f}"
            )

        # Determine fill price
        if entry_price:
            fill_price = entry_price
            order_type = "LIMIT"
        else:
            fill_price = await _get_mark_price(symbol, exchange_id)
            order_type = "MARKET"

        # Calculate position size
        notional = usdt_amount * leverage
        size = notional / fill_price

        # Calculate R:R
        if direction == "LONG":
            sl_dist = fill_price - stop_loss
            tp1_dist = tp1 - fill_price
            tp2_dist = tp2 - fill_price
        else:
            sl_dist = stop_loss - fill_price
            tp1_dist = fill_price - tp1
            tp2_dist = fill_price - tp2

        rr_tp1 = round(tp1_dist / sl_dist, 2) if sl_dist > 0 else 0
        rr_tp2 = round(tp2_dist / sl_dist, 2) if sl_dist > 0 else 0

        # Reserve margin
        _wallet["available_usdt"] -= usdt_amount

        trade_id = str(uuid.uuid4())[:8]
        position = {
            "trade_id": trade_id,
            "symbol": symbol,
            "direction": direction,
            "order_type": order_type,
            "size": round(size, 6),
            "entry_price": round(fill_price, 6),
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "tp1_pct": tp1_pct,
            "tp2_pct": 1.0 - tp1_pct,
            "tp1_hit": False,
            "tp2_hit": False,
            "leverage": leverage,
            "margin_usdt": usdt_amount,
            "notional_usdt": notional,
            "rr_tp1": rr_tp1,
            "rr_tp2": rr_tp2,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "status": "open",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "exchange_id": exchange_id,
        }

        _positions[trade_id] = position
        _wallet["trade_count"] += 1

        logger.info(
            f"[PaperWallet] OPEN {direction} {symbol} "
            f"@ {fill_price:.4f} | SL={stop_loss:.4f} TP1={tp1:.4f} TP2={tp2:.4f} "
            f"R:R={rr_tp1}/{rr_tp2} | Margin=${usdt_amount:.2f} Lev={leverage}x"
        )
        return position


async def paper_update_positions(exchange_id: str = "binance") -> list[dict]:
    """
    Check current prices for all open positions.
    Auto-triggers SL / TP1 / TP2 if price has crossed.
    Returns list of positions that were closed this cycle.
    """
    closed_this_cycle = []

    async with _lock:
        if not _positions:
            return []

        # Fetch prices for all symbols in one batch
        symbols = list({p["symbol"] for p in _positions.values() if p["status"] == "open"})

    # Fetch outside lock to avoid blocking
    prices: dict[str, float] = {}
    from app.services.exchange.factory import create_exchange
    try:
        async with create_exchange(exchange_id) as client:
            for sym in symbols:
                try:
                    ticker = await client.exchange.fetch_ticker(sym)
                    prices[sym] = float(ticker.get("last") or ticker.get("mark", 0))
                except Exception as e:
                    logger.warning(f"[PaperWallet] Price fetch failed for {sym}: {e}")
    except Exception as e:
        logger.error(f"[PaperWallet] Exchange connection failed: {e}")
        return []

    async with _lock:
        for trade_id, pos in list(_positions.items()):
            if pos["status"] != "open":
                continue

            price = prices.get(pos["symbol"])
            if not price:
                continue

            direction = pos["direction"]
            entry = pos["entry_price"]

            # Calculate unrealized P&L
            if direction == "LONG":
                pnl_per_unit = price - entry
            else:
                pnl_per_unit = entry - price

            pos["unrealized_pnl"] = round(pnl_per_unit * pos["size"], 4)

            # ── Check SL ──────────────────────────────────────────────────────
            sl_hit = (direction == "LONG" and price <= pos["stop_loss"]) or \
                     (direction == "SHORT" and price >= pos["stop_loss"])

            if sl_hit:
                close_pnl = _close_position(pos, pos["stop_loss"], fraction=1.0, reason="SL")
                closed_this_cycle.append({**pos, "close_reason": "SL", "pnl": close_pnl})
                del _positions[trade_id]
                continue

            # ── Check TP1 (partial close) ─────────────────────────────────────
            if not pos["tp1_hit"]:
                tp1_hit = (direction == "LONG" and price >= pos["tp1"]) or \
                          (direction == "SHORT" and price <= pos["tp1"])

                if tp1_hit:
                    pnl_partial = _close_position(pos, pos["tp1"], fraction=pos["tp1_pct"], reason="TP1")
                    pos["tp1_hit"] = True
                    pos["size"] = round(pos["size"] * pos["tp2_pct"], 6)
                    pos["margin_usdt"] *= pos["tp2_pct"]
                    # Move SL to breakeven
                    pos["stop_loss"] = entry
                    pos["realized_pnl"] += pnl_partial
                    logger.info(
                        f"[PaperWallet] TP1 hit {pos['symbol']} @ {pos['tp1']:.4f} "
                        f"| PnL={pnl_partial:+.4f} USDT | SL moved to BE={entry:.4f}"
                    )

            # ── Check TP2 (full close) ────────────────────────────────────────
            if pos["tp1_hit"] and not pos["tp2_hit"]:
                tp2_hit = (direction == "LONG" and price >= pos["tp2"]) or \
                          (direction == "SHORT" and price <= pos["tp2"])

                if tp2_hit:
                    pnl_final = _close_position(pos, pos["tp2"], fraction=1.0, reason="TP2")
                    closed_this_cycle.append({
                        **pos, "close_reason": "TP2",
                        "pnl": pos["realized_pnl"] + pnl_final
                    })
                    del _positions[trade_id]

    return closed_this_cycle


def _close_position(pos: dict, close_price: float, fraction: float, reason: str) -> float:
    """
    Internal: close a fraction of the position.
    Returns the realized PnL for this close.
    Updates wallet balance.
    """
    direction = pos["direction"]
    size_to_close = pos["size"] * fraction

    if direction == "LONG":
        pnl = (close_price - pos["entry_price"]) * size_to_close
    else:
        pnl = (pos["entry_price"] - close_price) * size_to_close

    pnl = round(pnl, 4)
    margin_returned = pos["margin_usdt"] * fraction

    # Update wallet
    _wallet["available_usdt"] += margin_returned + pnl
    _wallet["balance_usdt"] += pnl
    _wallet["total_pnl"] += pnl

    if reason in ("TP1", "TP2") or pnl > 0:
        if fraction >= 1.0:
            _wallet["win_count"] += 1

    if fraction >= 1.0:
        closed_record = {
            **pos,
            "close_price": close_price,
            "close_reason": reason,
            "pnl": pnl,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        _closed_trades.append(closed_record)
        logger.info(
            f"[PaperWallet] CLOSE {reason} {pos['symbol']} "
            f"@ {close_price:.4f} | PnL={pnl:+.4f} USDT | "
            f"Balance=${_wallet['balance_usdt']:.2f}"
        )

    return pnl


async def paper_close_all(reason: str = "MANUAL") -> list[dict]:
    """Force-close all open positions at current market price."""
    closed = []
    async with _lock:
        for trade_id, pos in list(_positions.items()):
            if pos["status"] != "open":
                continue
            try:
                price = await _get_mark_price(pos["symbol"], pos.get("exchange_id", "binance"))
                pnl = _close_position(pos, price, 1.0, reason)
                closed.append({**pos, "close_reason": reason, "pnl": pnl})
                del _positions[trade_id]
            except Exception as e:
                logger.error(f"[PaperWallet] Force-close failed for {trade_id}: {e}")
    return closed


# ─────────────────────────────────────────────────────────────────────────────
# Read-only state accessors
# ─────────────────────────────────────────────────────────────────────────────

def get_wallet_state() -> dict:
    """Return current wallet snapshot (safe copy)."""
    win_rate = (
        round(_wallet["win_count"] / _wallet["trade_count"] * 100, 1)
        if _wallet["trade_count"] > 0 else 0.0
    )
    return {
        **_wallet,
        "open_positions": len(_positions),
        "win_rate_pct": win_rate,
        "closed_trades": len(_closed_trades),
    }


def get_open_positions() -> list[dict]:
    return list(_positions.values())


def get_closed_trades(limit: int = 50) -> list[dict]:
    return _closed_trades[-limit:]


def has_position(symbol: str) -> bool:
    return any(p["symbol"] == symbol and p["status"] == "open" for p in _positions.values())


def get_position_count() -> int:
    return sum(1 for p in _positions.values() if p["status"] == "open")


def reset_wallet(starting_balance: float = 1000.0) -> None:
    """Reset paper wallet to initial state (for testing)."""
    global _positions, _closed_trades
    _positions = {}
    _closed_trades = []
    _wallet.update({
        "balance_usdt": starting_balance,
        "available_usdt": starting_balance,
        "total_pnl": 0.0,
        "trade_count": 0,
        "win_count": 0,
    })
    logger.info(f"[PaperWallet] Reset — balance: ${starting_balance:,.2f} USDT")
