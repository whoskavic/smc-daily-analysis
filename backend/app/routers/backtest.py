"""
Router: /api/backtest — run SMC backtests and inspect saved results.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.models.database import SessionLocal, BacktestRun
from app.services.backtest.engine import run_backtest, DEFAULT_FEES_PCT, DEFAULT_SLIPPAGE_PCT
from app.services.backtest.signal_simulator import SignalConfig
from app.services.backtest.trade_simulator import SimConfig

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["backtest"])

_DEFAULT_SIM = SimConfig()
_DEFAULT_SIGNAL = SignalConfig()


class BacktestRequest(BaseModel):
    symbol: str
    since: datetime                              # UTC, e.g. "2025-08-01T00:00:00Z"
    until: Optional[datetime] = None              # defaults to now
    init_cash: float = 1000.0
    fees_pct: float = DEFAULT_FEES_PCT            # sim_mode="vbt_legacy" only
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT
    claude_sample_pct: float = Field(0.0, ge=0.0, le=1.0)
    claude_sample_max: int = Field(0, ge=0)
    save: bool = True

    # ── Phase 6: event-driven simulation options (all optional) ─────────────
    sim_mode: Literal["event", "vbt_legacy"] = "event"
    maker_fee_pct: float = Field(_DEFAULT_SIM.maker_fee_pct, ge=0)
    taker_fee_pct: float = Field(_DEFAULT_SIM.taker_fee_pct, ge=0)
    order_ttl_bars: int = Field(_DEFAULT_SIM.order_ttl_bars, gt=0)
    sizing_mode: Literal["risk_pct", "fixed_risk_usdt", "fixed_margin_usdt"] = _DEFAULT_SIM.sizing_mode
    # risk_pct/leverage default to None so the live-mirroring settings
    # (risk_per_trade_pct / max_leverage) are used unless explicitly overridden.
    risk_pct: Optional[float] = Field(None, gt=0)
    fixed_risk_usdt: float = Field(_DEFAULT_SIM.fixed_risk_usdt, gt=0)
    fixed_margin_usdt: float = Field(_DEFAULT_SIM.fixed_margin_usdt, gt=0)
    leverage: Optional[int] = Field(None, gt=0)

    # ── Phase 6: rule-based proxy risk floor + decision cadence (optional) ──
    min_sl_pct: Optional[float] = Field(_DEFAULT_SIGNAL.min_sl_pct, gt=0)
    min_sl_atr: Optional[float] = Field(_DEFAULT_SIGNAL.min_sl_atr, gt=0)
    decision_schedule: Literal["every_bar", "daily"] = "every_bar"


@router.post("/run")
async def run_backtest_endpoint(req: BacktestRequest):
    """Run a backtest synchronously and (by default) persist the result."""
    since = req.since if req.since.tzinfo else req.since.replace(tzinfo=timezone.utc)
    until = req.until
    if until and not until.tzinfo:
        until = until.replace(tzinfo=timezone.utc)

    sim_config = None
    if req.sim_mode == "event":
        from app.config import settings
        risk_pct = req.risk_pct if req.risk_pct is not None else getattr(settings, "risk_per_trade_pct", 1.0)
        leverage = req.leverage if req.leverage is not None else getattr(settings, "max_leverage", 10)
        sim_config = SimConfig(
            init_cash=req.init_cash,
            maker_fee_pct=req.maker_fee_pct,
            taker_fee_pct=req.taker_fee_pct,
            slippage_pct=req.slippage_pct,
            order_ttl_bars=req.order_ttl_bars,
            sizing_mode=req.sizing_mode,
            risk_pct=risk_pct,
            fixed_risk_usdt=req.fixed_risk_usdt,
            fixed_margin_usdt=req.fixed_margin_usdt,
            leverage=leverage,
        )

    signal_config = SignalConfig(min_sl_pct=req.min_sl_pct, min_sl_atr=req.min_sl_atr)

    try:
        result = run_backtest(
            symbol=req.symbol,
            since=since,
            until=until,
            init_cash=req.init_cash,
            fees_pct=req.fees_pct,
            slippage_pct=req.slippage_pct,
            claude_sample_pct=req.claude_sample_pct,
            claude_sample_max=req.claude_sample_max,
            sim_mode=req.sim_mode,
            sim_config=sim_config,
            signal_config=signal_config,
            decision_schedule=req.decision_schedule,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[Backtest] run failed for {req.symbol}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Backtest failed: {e}")

    run_id = None
    if req.save:
        run_id = _save_run(req, result)

    return {"run_id": run_id, **result}


@router.get("/runs")
async def list_backtest_runs(symbol: Optional[str] = None, limit: int = 20):
    """List saved backtest runs, most recent first."""
    db = SessionLocal()
    try:
        q = db.query(BacktestRun)
        if symbol:
            q = q.filter(BacktestRun.symbol == symbol)
        rows = q.order_by(BacktestRun.id.desc()).limit(limit).all()
        return {
            "runs": [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "since": r.since.isoformat() if r.since else None,
                    "until": r.until.isoformat() if r.until else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "total_trades": r.total_trades,
                    "win_rate_pct": r.win_rate_pct,
                    "total_return_pct": r.total_return_pct,
                    "sharpe_ratio": r.sharpe_ratio,
                    "max_drawdown_pct": r.max_drawdown_pct,
                    "profit_factor": r.profit_factor,
                }
                for r in rows
            ]
        }
    finally:
        db.close()


@router.get("/runs/{run_id}")
async def get_backtest_run(run_id: int):
    """Full detail of one saved backtest run, including trades and equity curve."""
    db = SessionLocal()
    try:
        r = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
        if not r:
            raise HTTPException(status_code=404, detail=f"Backtest run {run_id} not found")
        return {
            "id": r.id,
            "symbol": r.symbol,
            "since": r.since.isoformat() if r.since else None,
            "until": r.until.isoformat() if r.until else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "init_cash": r.init_cash,
            "fees_pct": r.fees_pct,
            "slippage_pct": r.slippage_pct,
            "claude_sample_pct": r.claude_sample_pct,
            "bars_analyzed": r.bars_analyzed,
            "signals_generated": r.signals_generated,
            "total_trades": r.total_trades,
            "final_equity": r.final_equity,
            "total_return_pct": r.total_return_pct,
            "win_rate_pct": r.win_rate_pct,
            "sharpe_ratio": r.sharpe_ratio,
            "max_drawdown_pct": r.max_drawdown_pct,
            "profit_factor": r.profit_factor,
            "trades": r.trades,
            "equity_curve": r.equity_curve,
            "claude_sample": r.claude_sample,
            "note": r.note,
        }
    finally:
        db.close()


def _sim_note(req: BacktestRequest, result: dict) -> Optional[str]:
    """sim_mode/sizing_mode summary — BacktestRun has no dedicated columns for
    these, so they ride in the existing `note` text column instead."""
    if req.sim_mode == "event":
        tag = f"sim_mode=event sizing_mode={req.sizing_mode}"
    else:
        tag = "sim_mode=vbt_legacy"
    existing = result.get("note")
    return f"{tag} | {existing}" if existing else tag


def _save_run(req: BacktestRequest, result: dict) -> int:
    db = SessionLocal()
    try:
        row = BacktestRun(
            symbol=req.symbol,
            since=req.since.replace(tzinfo=None) if req.since.tzinfo else req.since,
            until=(req.until or datetime.now(timezone.utc)).replace(tzinfo=None),
            init_cash=req.init_cash,
            fees_pct=req.fees_pct,
            slippage_pct=req.slippage_pct,
            claude_sample_pct=req.claude_sample_pct,
            bars_analyzed=result.get("bars_analyzed", 0),
            signals_generated=result.get("signals_generated", 0),
            total_trades=result.get("total_trades", 0),
            final_equity=result.get("final_equity"),
            total_return_pct=result.get("total_return_pct", 0.0),
            win_rate_pct=result.get("win_rate_pct", 0.0),
            sharpe_ratio=result.get("sharpe_ratio", 0.0),
            max_drawdown_pct=result.get("max_drawdown_pct", 0.0),
            profit_factor=result.get("profit_factor"),
            trades=result.get("trades", []),
            equity_curve=result.get("equity_curve", []),
            claude_sample=result.get("claude_sample", []),
            note=_sim_note(req, result),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()
