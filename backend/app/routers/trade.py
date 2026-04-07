from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from app.models.database import get_db, TradeHistory
from app.services import trading_service
from app.config import settings

router = APIRouter(prefix="/api/trade", tags=["trade"])


class ExecuteTradeRequest(BaseModel):
    symbol: str
    direction: str          # LONG or SHORT
    usdt_amount: float      # USDT to risk (e.g. 50)
    entry_price: Optional[float] = None   # None = market order
    stop_loss: float
    take_profit: float
    leverage: int = 10
    analysis_id: Optional[int] = None
    notes: Optional[str] = None


@router.get("/balance")
def get_balance():
    """Get USDT futures wallet balance."""
    if not settings.binance_api_key:
        raise HTTPException(status_code=400, detail="Binance API key not configured")
    try:
        return trading_service.get_account_balance()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/positions")
def get_positions():
    """Get all open futures positions."""
    if not settings.binance_api_key:
        raise HTTPException(status_code=400, detail="Binance API key not configured")
    try:
        return trading_service.get_positions()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/history")
def get_trade_history(limit: int = 50, db: Session = Depends(get_db)):
    """Get local trade history (all trades executed through this app)."""
    trades = (
        db.query(TradeHistory)
        .order_by(TradeHistory.executed_at.desc())
        .limit(limit)
        .all()
    )
    return [_serialize(t) for t in trades]


@router.post("/execute")
def execute_trade(req: ExecuteTradeRequest, db: Session = Depends(get_db)):
    """
    Execute a trade on Binance Futures.
    Places entry order + stop-loss + take-profit simultaneously.
    """
    if not settings.binance_api_key:
        raise HTTPException(status_code=400, detail="Binance API key not configured")

    if req.direction not in ("LONG", "SHORT"):
        raise HTTPException(status_code=400, detail="direction must be LONG or SHORT")

    if req.usdt_amount <= 0:
        raise HTTPException(status_code=400, detail="usdt_amount must be positive")

    if req.leverage < 1 or req.leverage > 125:
        raise HTTPException(status_code=400, detail="leverage must be 1-125")

    try:
        result = trading_service.execute_trade_plan(
            symbol=req.symbol,
            direction=req.direction,
            usdt_amount=req.usdt_amount,
            entry_price=req.entry_price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            leverage=req.leverage,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Save to local trade history
    trade = TradeHistory(
        symbol=result["symbol"],
        direction=result["direction"],
        order_type=result["order_type"],
        quantity=result["quantity"],
        entry_price=result["entry_price"],
        stop_loss=result["stop_loss"],
        take_profit=result["take_profit"],
        leverage=result["leverage"],
        usdt_amount=result["usdt_amount"],
        entry_order_id=str(result.get("entry_order_id", "")),
        sl_order_id=str(result.get("sl_order_id", "")),
        tp_order_id=str(result.get("tp_order_id", "")),
        status="open",
        analysis_id=req.analysis_id,
        notes=req.notes,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    return {**result, "trade_id": trade.id}


@router.patch("/history/{trade_id}/close")
def close_trade(
    trade_id: int,
    close_price: float,
    db: Session = Depends(get_db),
):
    """Mark a trade as closed and record PnL."""
    trade = db.query(TradeHistory).filter_by(id=trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    pnl_per_unit = (close_price - trade.entry_price) * (1 if trade.direction == "LONG" else -1)
    pnl = pnl_per_unit * trade.quantity

    trade.close_price = close_price
    trade.pnl = round(pnl, 4)
    trade.status = "closed"
    trade.closed_at = datetime.utcnow()
    db.commit()

    return _serialize(trade)


def _serialize(t: TradeHistory) -> dict:
    return {
        "id": t.id,
        "symbol": t.symbol,
        "direction": t.direction,
        "order_type": t.order_type,
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "stop_loss": t.stop_loss,
        "take_profit": t.take_profit,
        "leverage": t.leverage,
        "usdt_amount": t.usdt_amount,
        "entry_order_id": t.entry_order_id,
        "sl_order_id": t.sl_order_id,
        "tp_order_id": t.tp_order_id,
        "status": t.status,
        "pnl": t.pnl,
        "close_price": t.close_price,
        "analysis_id": t.analysis_id,
        "executed_at": t.executed_at.isoformat() if t.executed_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "notes": t.notes,
    }
