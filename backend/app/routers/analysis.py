from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date

from app.models.database import get_db, DailyAnalysis
from app.services.binance_service import fetch_market_snapshot, fetch_ticker
from app.services.claude_service import run_analysis
from app.config import settings

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.get("/symbols")
def get_watched_symbols():
    """Return the list of symbols being tracked."""
    return {"symbols": settings.watch_symbols}


@router.get("/latest")
def get_latest_analyses(db: Session = Depends(get_db)):
    """Get the most recent analysis for each watched symbol."""
    results = []
    for symbol in settings.watch_symbols:
        record = (
            db.query(DailyAnalysis)
            .filter_by(symbol=symbol)
            .order_by(DailyAnalysis.created_at.desc())
            .first()
        )
        if record:
            results.append(_serialize(record))
    return results


@router.get("/{symbol}")
def get_analysis_for_symbol(
    symbol: str,
    limit: int = 7,
    db: Session = Depends(get_db),
):
    """Get recent analyses for a symbol (URL-encoded, e.g. BTC%2FUSDT)."""
    symbol = symbol.replace("%2F", "/").upper()
    records = (
        db.query(DailyAnalysis)
        .filter_by(symbol=symbol)
        .order_by(DailyAnalysis.analysis_date.desc())
        .limit(limit)
        .all()
    )
    return [_serialize(r) for r in records]


@router.post("/run/{symbol}")
async def trigger_analysis(
    symbol: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Manually trigger analysis for a symbol. Runs in the background."""
    symbol = symbol.replace("%2F", "/").upper()
    if symbol not in settings.watch_symbols:
        raise HTTPException(status_code=400, detail=f"{symbol} is not in watch list")

    background_tasks.add_task(_run_and_save, symbol, db)
    return {"status": "queued", "symbol": symbol}


@router.get("/ticker/{symbol}")
def get_ticker(symbol: str):
    """Live price ticker for a symbol."""
    symbol = symbol.replace("%2F", "/").upper()
    try:
        return fetch_ticker(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


def _run_and_save(symbol: str, db: Session):
    today = date.today().isoformat()
    snapshot = fetch_market_snapshot(symbol)
    result = run_analysis(snapshot)
    # Upsert: delete existing today's record then insert fresh
    db.query(DailyAnalysis).filter_by(symbol=symbol, analysis_date=today).delete()
    record = DailyAnalysis(
        symbol=result["symbol"],
        analysis_date=today,
        open_price=result.get("open_price"),
        high_price=result.get("high_price"),
        low_price=result.get("low_price"),
        close_price=result.get("close_price"),
        volume=result.get("volume"),
        funding_rate=result.get("funding_rate"),
        fear_greed_index=result.get("fear_greed_index"),
        bias=result["bias"],
        confidence=result["confidence"],
        key_levels=result.get("key_levels", []),
        trade_idea=result.get("trade_idea", ""),
        full_analysis=result.get("full_analysis", ""),
        raw_prompt=result.get("raw_prompt", ""),
    )
    db.add(record)
    db.commit()


def _serialize(r: DailyAnalysis) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "analysis_date": r.analysis_date,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "bias": r.bias,
        "confidence": r.confidence,
        "key_levels": r.key_levels or [],
        "trade_idea": r.trade_idea,
        "full_analysis": r.full_analysis,
        "open_price": r.open_price,
        "high_price": r.high_price,
        "low_price": r.low_price,
        "close_price": r.close_price,
        "volume": r.volume,
        "funding_rate": r.funding_rate,
        "fear_greed_index": r.fear_greed_index,
    }
