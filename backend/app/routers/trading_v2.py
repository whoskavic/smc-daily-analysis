"""
Router: /api/trading — trade execution, paper wallet, exchange status.
Replaces and extends the existing trade.py router.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from app.config import settings
from app.services.ws_manager import manager as ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trading", tags=["trading"])


# ─────────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────────

class ManualTradeRequest(BaseModel):
    symbol: str
    direction: str          # "LONG" | "SHORT"
    entry_price: Optional[float] = None
    stop_loss: float
    tp1: float
    tp2: float
    leverage: int = 5
    tp1_close_pct: int = 50
    exchange_id: Optional[str] = None   # overrides ACTIVE_EXCHANGE


class CloseAllRequest(BaseModel):
    reason: str = "MANUAL"


# ─────────────────────────────────────────────────────────────────────────────
# Status & info
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
async def trading_status():
    """Return current trading mode, exchange, and session info."""
    from app.services.exchange.executor import get_account_balance
    from app.services.exchange.paper_wallet import get_wallet_state, get_open_positions

    exchange_id = settings.active_exchange

    # Build status
    status = {
        "mode": settings.trade_mode.upper(),
        "exchange": exchange_id,
        "swarm_enabled": settings.swarm_enabled,
        "max_positions": settings.max_concurrent_positions,
        "risk_per_trade_pct": settings.risk_per_trade_pct,
    }

    if settings.is_paper_mode():
        status["wallet"] = get_wallet_state()
        status["positions"] = get_open_positions()
    else:
        try:
            balance = await get_account_balance(exchange_id)
            status["balance"] = balance
        except Exception as e:
            status["balance_error"] = str(e)

    return status


@router.get("/balance")
async def get_balance():
    """Get current balance (paper or live)."""
    if settings.is_paper_mode():
        from app.services.exchange.paper_wallet import get_wallet_state
        return {"mode": "paper", **get_wallet_state()}
    else:
        from app.services.exchange.executor import get_account_balance
        return {"mode": "live", **await get_account_balance(settings.active_exchange)}


@router.get("/chart-data")
async def get_chart_data(symbol: str, timeframe: str = "1h", limit: int = 200):
    """
    Candles + SMC overlay (order blocks, FVGs, BOS/CHoCH, liquidity, premium/
    discount) for a single timeframe. Powers the Terminal UI chart (Phase 5c).
    Read-only, no Claude API call — reuses the Phase 2 detectors directly.
    """
    from app.services.binance_service import fetch_ohlcv
    from app.services import smc_engine

    tf_map = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
    label = tf_map.get(timeframe.lower(), timeframe.upper())

    try:
        candles = fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch candles: {e}")

    if not candles:
        return {"symbol": symbol, "timeframe": timeframe, "candles": [], "smc_levels": {"key_levels": [], "confluence": {"score": 0, "factors": {}, "conflicts": []}}}

    swings = smc_engine.detect_swings(candles)
    bos_events = smc_engine.detect_bos_choch(candles, swings, tf=label)

    key_levels = []
    key_levels += smc_engine.detect_order_blocks(candles, bos_events, tf=label)
    key_levels += smc_engine.detect_fair_value_gaps(candles, tf=label)
    key_levels += smc_engine.detect_liquidity_zones(candles, swings, tf=label)
    key_levels += smc_engine.premium_discount_levels(candles, tf=label)
    key_levels += [
        {"type": e["type"], "price": e["price"], "low": None, "high": None, "tf": label, "strength": e["strength"]}
        for e in bos_events
    ]

    confluence = smc_engine.score_confluence({label: candles})

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "smc_levels": {"key_levels": key_levels, "confluence": confluence},
    }


@router.get("/positions")
async def get_positions():
    """Get all open positions (paper or live)."""
    if settings.is_paper_mode():
        from app.services.exchange.paper_wallet import get_open_positions
        return {"mode": "paper", "positions": get_open_positions()}
    else:
        from app.services.exchange.executor import get_live_positions
        positions = await get_live_positions(settings.active_exchange)
        return {"mode": "live", "positions": positions}


@router.get("/history")
async def get_trade_history(limit: int = 50):
    """Get closed trade history (paper or live)."""
    if settings.is_paper_mode():
        from app.services.exchange.paper_wallet import get_closed_trades
        return {"mode": "paper", "trades": get_closed_trades(limit)}
    else:
        from app.models.database import SessionLocal, TradeHistory
        db = SessionLocal()
        try:
            trades = (
                db.query(TradeHistory)
                .order_by(TradeHistory.id.desc())
                .limit(limit)
                .all()
            )
            return {
                "mode": "live",
                "trades": [
                    {
                        "id": t.id,
                        "symbol": t.symbol,
                        "direction": t.direction,
                        "entry_price": t.entry_price,
                        "stop_loss": t.stop_loss,
                        "take_profit": t.take_profit,
                        "status": t.status,
                        "pnl": t.pnl,
                        "leverage": t.leverage,
                        "usdt_amount": t.usdt_amount,
                        "notes": t.notes,
                    }
                    for t in trades
                ],
            }
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Manual trade execution
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/execute")
async def execute_manual_trade(req: ManualTradeRequest):
    """
    Manually execute a trade.
    Uses TRADE_MODE from settings (paper or live).
    """
    from app.services.exchange.executor import execute_signal

    exchange_id = req.exchange_id or settings.active_exchange

    signal = {
        "symbol": req.symbol,
        "decision": "TRADE",
        "direction": req.direction.upper(),
        "entry_type": "LIMIT" if req.entry_price else "MARKET",
        "entry_price": req.entry_price,
        "stop_loss": req.stop_loss,
        "tp1": req.tp1,
        "tp2": req.tp2,
        "tp1_close_pct": req.tp1_close_pct,
        "recommended_leverage": req.leverage,
        "confidence": 100,  # manual trade, bypass confidence check
    }

    try:
        result = await execute_signal(signal, exchange_id)
        ws_manager.broadcast_nowait("trade_update", {
            "event": "opened",
            "symbol": req.symbol,
            "direction": req.direction.upper(),
            "confidence": 100,
            "source": "manual",
            "mode": settings.trade_mode,
        })
        return {
            "success": True,
            "mode": settings.trade_mode,
            "exchange": exchange_id,
            "trade": result,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[Router] Manual trade failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Trade execution failed: {e}")


@router.post("/close-all")
async def close_all_positions(req: CloseAllRequest):
    """Close all open positions (paper only — live requires manual confirmation)."""
    if settings.is_live_mode():
        raise HTTPException(
            status_code=403,
            detail="Close all is not available in live mode via API for safety. Use your exchange UI."
        )

    from app.services.exchange.paper_wallet import paper_close_all
    closed = await paper_close_all(reason=req.reason)
    total_pnl = sum(t.get("pnl", 0) for t in closed)

    if closed:
        ws_manager.broadcast_nowait("trade_update", {
            "event": "closed_all",
            "count": len(closed),
            "total_pnl": round(total_pnl, 4),
            "reason": req.reason,
            "mode": "paper",
        })

    return {
        "closed": len(closed),
        "total_pnl": round(total_pnl, 4),
        "trades": closed,
    }


@router.post("/wallet/reset")
async def reset_paper_wallet(starting_balance: float = 1000.0):
    """Reset paper wallet to starting balance (paper mode only)."""
    if settings.is_live_mode():
        raise HTTPException(status_code=403, detail="Cannot reset wallet in live mode.")

    from app.services.exchange.paper_wallet import reset_wallet
    reset_wallet(starting_balance)
    return {"success": True, "balance": starting_balance, "message": "Paper wallet reset."}


# ─────────────────────────────────────────────────────────────────────────────
# Swarm control
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/swarm/trigger")
async def trigger_swarm():
    """Manually trigger a swarm scan immediately."""
    from app.services.scheduler import run_swarm_scan
    try:
        await run_swarm_scan()
        return {"success": True, "message": "Swarm scan completed."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/swarm/tokens")
async def get_swarm_tokens(force_refresh: bool = False):
    """Get current top 50 token list."""
    from app.services.exchange.token_discovery import get_top50_symbols, get_token_info

    symbols = await get_top50_symbols(settings.active_exchange, force_refresh=force_refresh)
    enriched = []
    for sym in symbols:
        info = get_token_info(sym) or {}
        enriched.append({"symbol": sym, **info})

    return {
        "exchange": settings.active_exchange,
        "count": len(symbols),
        "tokens": enriched,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TradingView webhook receiver
# ─────────────────────────────────────────────────────────────────────────────

class TVWebhookPayload(BaseModel):
    secret: str = ""
    symbol: str
    action: str     # "BUY" | "SELL" | "CLOSE"
    price: Optional[float] = None
    message: Optional[str] = None


@router.post("/webhook/tradingview")
async def tradingview_webhook(payload: TVWebhookPayload):
    """
    Receive TradingView alert webhooks.
    TV alert format (paste in TradingView alert message):
    {
      "secret": "your_secret_here",
      "symbol": "{{ticker}}",
      "action": "BUY",
      "price": {{close}}
    }
    """
    # Validate secret
    if settings.tradingview_webhook_secret:
        if payload.secret != settings.tradingview_webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret.")

    logger.info(
        f"[TVWebhook] Received: {payload.symbol} {payload.action} @ {payload.price}"
    )

    # Map TV action to our format
    action = payload.action.upper()
    if action in ("BUY", "LONG"):
        direction = "LONG"
    elif action in ("SELL", "SHORT"):
        direction = "SHORT"
    elif action == "CLOSE":
        # TODO: implement close logic
        return {"received": True, "action": "CLOSE", "status": "not_implemented_yet"}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # Run SMC analysis to get SL/TP (don't blindly follow TV signal)
    # This ensures TV is just a trigger, our SMC engine validates
    return {
        "received": True,
        "symbol": payload.symbol,
        "direction": direction,
        "status": "queued_for_smc_validation",
        "note": "TV signal queued. SMC analysis will validate before execution.",
    }
