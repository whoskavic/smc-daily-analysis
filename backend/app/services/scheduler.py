"""
Scheduler v2 — Multi-exchange, Swarm, Paper/Live mode.

Jobs:
  1. daily_htf_bias    — 08:00 WIB daily. Full SMC analysis on watch_symbols.
  2. swarm_scan        — Every 15 min during London/NY sessions (kill zones).
  3. paper_update      — Every 60 s: update paper positions, check SL/TP hits.
  4. token_refresh     — Every 24H: refresh Top 50 market cap list.
  5. trade_sync        — Every 60 s (live mode only): sync real exchange positions.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.models.database import SessionLocal, DailyAnalysis, TradeHistory
from app.services.session_utils import KillZones, current_session as _current_session, is_active_session as _is_active_session
from app.services.ws_manager import manager as ws_manager

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


# ─────────────────────────────────────────────────────────────────────────────
# Job 1: Daily HTF bias analysis
# ─────────────────────────────────────────────────────────────────────────────

async def run_daily_analysis():
    """Full SMC analysis on watch_symbols at scheduled time."""
    from app.services.snapshot_builder import build_enriched_snapshot
    from app.services.claude_service import run_analysis
    from app.services.exchange.executor import execute_signal

    today = datetime.now(timezone.utc).date().isoformat()
    exchange_id = settings.active_exchange
    symbols = settings.watch_symbols

    logger.info(f"[Scheduler] Daily analysis — {today} — {len(symbols)} symbols on {exchange_id}")

    for symbol in symbols:
        db = SessionLocal()
        try:
            existing = db.query(DailyAnalysis).filter_by(symbol=symbol, analysis_date=today).first()
            if existing:
                logger.info(f"[Scheduler] {symbol}: already analyzed today, skipping.")
                continue

            logger.info(f"[Scheduler] Analyzing {symbol}...")
            snapshot = build_enriched_snapshot(symbol)
            result = run_analysis(snapshot)

            record = DailyAnalysis(
                symbol=result["symbol"],
                analysis_date=today,
                bias=result["bias"],
                confidence=result.get("confidence"),
                key_levels=result.get("key_levels", []),
                trade_idea=result.get("trade_idea", ""),
                full_analysis=result.get("full_analysis", ""),
                raw_prompt=result.get("raw_prompt", ""),
                trade_direction=result.get("trade_direction"),
                trade_entry=result.get("trade_entry"),
                trade_sl=result.get("trade_sl"),
                trade_tp=result.get("trade_tp"),
                open_price=result.get("open_price"),
                high_price=result.get("high_price"),
                low_price=result.get("low_price"),
                close_price=result.get("close_price"),
                volume=result.get("volume"),
                funding_rate=result.get("funding_rate"),
                fear_greed_index=result.get("fear_greed_index"),
            )
            db.add(record)
            db.commit()
            db.refresh(record)

            ws_manager.broadcast_nowait("analysis_update", {
                "symbol": record.symbol,
                "bias": record.bias,
                "confidence": record.confidence,
                "trade_direction": record.trade_direction,
            })

            # Auto-execute if signal is strong enough
            execution = result.get("execution", {})
            if (
                execution.get("decision") == "TRADE"
                and execution.get("confidence", 0) >= settings.swarm_min_confidence
            ):
                logger.info(f"[Scheduler] Auto-executing signal for {symbol}...")
                try:
                    trade_result = await execute_signal(execution, exchange_id)
                    logger.info(
                        f"[Scheduler] Trade {'placed (paper)' if settings.is_paper_mode() else 'EXECUTED'}: "
                        f"{symbol} {execution.get('direction')} @ {execution.get('entry_price')}"
                    )
                    # Persist trade record
                    _save_trade_record(db, record, execution, trade_result)
                    ws_manager.broadcast_nowait("trade_update", {
                        "event": "opened",
                        "symbol": symbol,
                        "direction": execution.get("direction"),
                        "confidence": execution.get("confidence"),
                        "source": "daily_analysis",
                        "mode": "paper" if settings.is_paper_mode() else "live",
                    })
                except Exception as e:
                    logger.error(f"[Scheduler] Auto-execute failed for {symbol}: {e}")
            else:
                reason = execution.get("no_trade_reason") or execution.get("wait_for") or "NO TRADE"
                logger.info(f"[Scheduler] {symbol}: {reason}")

        except Exception as e:
            logger.error(f"[Scheduler] Error analyzing {symbol}: {e}", exc_info=True)
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Job 2: Swarm scan (kill zones only)
# ─────────────────────────────────────────────────────────────────────────────

_swarm_running = False   # prevent overlapping runs


async def run_swarm_scan():
    """Scan top 50 tokens for SMC setups during active sessions."""
    global _swarm_running

    if not settings.swarm_enabled:
        return

    if not _is_active_session():
        logger.debug(f"[Swarm] Dead zone ({_current_session()}) — skipping scan")
        return

    if _swarm_running:
        logger.warning("[Swarm] Previous scan still running — skipping this cycle")
        return

    _swarm_running = True
    try:
        from app.services.exchange.token_discovery import get_top50_symbols, get_cached_symbols
        from app.services.exchange.executor import execute_signal, get_live_positions
        from app.services.exchange.paper_wallet import get_position_count, get_wallet_state

        exchange_id = settings.active_exchange
        symbols = get_cached_symbols() or await get_top50_symbols(exchange_id)

        if not symbols:
            logger.warning("[Swarm] No symbols available — skipping")
            return

        session = _current_session()
        logger.info(f"[Swarm] Scanning {len(symbols)} tokens | session={session} | exchange={exchange_id}")
        ws_manager.broadcast_nowait("swarm_scan_start", {
            "total": len(symbols),
            "session": session,
            "exchange": exchange_id,
        })

        # Count current positions (paper or live)
        if settings.is_paper_mode():
            current_positions = get_position_count()
        else:
            live_pos = await get_live_positions(exchange_id)
            current_positions = len(live_pos)

        available_slots = settings.max_concurrent_positions - current_positions
        if available_slots <= 0:
            logger.info(f"[Swarm] Max positions ({settings.max_concurrent_positions}) reached — scanning only, not executing")

        # Import here to avoid circular at module load
        from app.services.snapshot_builder import build_enriched_snapshot, has_setup
        from app.services.claude_service import run_analysis

        semaphore = asyncio.Semaphore(settings.swarm_concurrency)
        results = []
        skipped_no_setup = 0

        async def scan_one(symbol: str):
            nonlocal skipped_no_setup
            async with semaphore:
                ws_manager.broadcast_nowait("swarm_token_update", {"symbol": symbol, "status": "scanning"})
                try:
                    snapshot = build_enriched_snapshot(symbol)
                    if not has_setup(snapshot):
                        skipped_no_setup += 1
                        logger.debug(f"[Swarm] {symbol}: no structural setup — skipping Claude call")
                        ws_manager.broadcast_nowait("swarm_token_update", {"symbol": symbol, "status": "no_setup"})
                        return
                    result = run_analysis(snapshot)
                    execution = result.get("execution", {})
                    result["execution"] = execution
                    results.append(result)
                    logger.debug(
                        f"[Swarm] {symbol}: bias={result.get('bias')} "
                        f"confidence={execution.get('confidence', 0)} "
                        f"decision={execution.get('decision', 'N/A')}"
                    )
                    ws_manager.broadcast_nowait("swarm_token_update", {
                        "symbol": symbol,
                        "status": "analyzed",
                        "bias": result.get("bias"),
                        "confidence": execution.get("confidence", 0),
                        "decision": execution.get("decision", "N/A"),
                    })
                except Exception as e:
                    logger.warning(f"[Swarm] {symbol} scan failed: {e}")
                    ws_manager.broadcast_nowait("swarm_token_update", {
                        "symbol": symbol, "status": "error", "error": str(e),
                    })

        await asyncio.gather(*[scan_one(s) for s in symbols])

        # Filter tradeable signals, sort by confidence
        tradeable = [
            r for r in results
            if r.get("execution", {}).get("decision") == "TRADE"
            and r.get("execution", {}).get("confidence", 0) >= settings.swarm_min_confidence
        ]
        tradeable.sort(key=lambda r: r["execution"]["confidence"], reverse=True)

        logger.info(
            f"[Swarm] Scan complete: {len(symbols)} scanned, "
            f"{skipped_no_setup} skipped (no structural setup), "
            f"{len(tradeable)} tradeable signals found"
        )

        # Execute top N signals (up to available slots)
        executed = 0
        for signal in tradeable:
            if executed >= available_slots:
                break
            sym = signal["symbol"]
            execution = signal["execution"]
            try:
                trade_result = await execute_signal(execution, exchange_id)
                logger.info(
                    f"[Swarm] {'PAPER' if settings.is_paper_mode() else 'LIVE'} "
                    f"trade: {sym} {execution['direction']} "
                    f"confidence={execution['confidence']}%"
                )
                ws_manager.broadcast_nowait("trade_update", {
                    "event": "opened",
                    "symbol": sym,
                    "direction": execution["direction"],
                    "confidence": execution["confidence"],
                    "source": "swarm",
                    "mode": "paper" if settings.is_paper_mode() else "live",
                })
                executed += 1
            except Exception as e:
                logger.error(f"[Swarm] Execute failed for {sym}: {e}")

        logger.info(f"[Swarm] Cycle complete — {executed} trades placed")
        ws_manager.broadcast_nowait("swarm_scan_complete", {
            "scanned": len(symbols),
            "skipped_no_setup": skipped_no_setup,
            "tradeable": len(tradeable),
            "executed": executed,
        })

    finally:
        _swarm_running = False


# ─────────────────────────────────────────────────────────────────────────────
# Job 3: Paper position updater
# ─────────────────────────────────────────────────────────────────────────────

async def update_paper_positions():
    """Advance pending LIMIT orders (fill/cancel), then check current
    prices and trigger SL/TP1/TP2 for paper positions. Pending orders are
    advanced first so a fill this tick only becomes eligible for its own
    SL/TP checks starting next tick (see paper_update_pending_orders'
    docstring) — both run on this job's existing 60s interval."""
    if not settings.is_paper_mode():
        return

    from app.services.exchange.paper_wallet import (
        paper_update_positions, paper_update_pending_orders,
        get_position_count, get_pending_order_count,
    )

    if get_pending_order_count() > 0:
        try:
            order_events = await paper_update_pending_orders(exchange_id=settings.active_exchange)
            for evt in order_events:
                if evt["event"] == "filled":
                    pos = evt["position"]
                    logger.info(
                        f"[Paper] Order filled: {evt['symbol']} {evt['direction']} "
                        f"@ {pos['entry_price']:.4f}"
                    )
                    _update_trade_record_on_fill(evt["order_id"], pos)
                    ws_manager.broadcast_nowait("trade_update", {
                        "event": "order_filled",
                        "symbol": evt["symbol"],
                        "direction": evt["direction"],
                        "entry_price": pos["entry_price"],
                        "mode": "paper",
                    })
                else:
                    logger.info(
                        f"[Paper] Order cancelled: {evt['symbol']} {evt['direction']} "
                        f"reason={evt['reason']}"
                    )
                    _update_trade_record_on_cancel(evt["order_id"], evt["reason"])
                    ws_manager.broadcast_nowait("trade_update", {
                        "event": "order_cancelled",
                        "symbol": evt["symbol"],
                        "direction": evt["direction"],
                        "reason": evt["reason"],
                        "mode": "paper",
                    })
        except Exception as e:
            logger.error(f"[Paper] Pending order update error: {e}", exc_info=True)

    if get_position_count() == 0:
        return

    try:
        closed = await paper_update_positions(exchange_id=settings.active_exchange)
        if closed:
            for pos in closed:
                logger.info(
                    f"[Paper] Position closed: {pos['symbol']} {pos['direction']} "
                    f"via {pos['close_reason']} | PnL={pos['pnl']:+.4f} USDT"
                )
                ws_manager.broadcast_nowait("trade_update", {
                    "event": "closed",
                    "symbol": pos["symbol"],
                    "direction": pos["direction"],
                    "close_reason": pos["close_reason"],
                    "pnl": pos["pnl"],
                    "mode": "paper",
                })
    except Exception as e:
        logger.error(f"[Paper] Position update error: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Job 4: Token list refresh
# ─────────────────────────────────────────────────────────────────────────────

async def refresh_token_list():
    """Refresh Top 50 market cap list from CoinGecko daily."""
    from app.services.exchange.token_discovery import get_top50_symbols
    try:
        symbols = await get_top50_symbols(
            exchange_id=settings.active_exchange,
            force_refresh=True,
        )
        logger.info(f"[TokenRefresh] Updated: {len(symbols)} tokens")
    except Exception as e:
        logger.error(f"[TokenRefresh] Failed: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Job 5: Live trade sync (live mode only)
# ─────────────────────────────────────────────────────────────────────────────

async def sync_live_trades():
    """Sync live exchange positions to local DB (live mode only)."""
    if settings.is_paper_mode():
        return

    if not getattr(settings, "binance_api_key", ""):
        return

    # Keep existing Binance sync for backward compatibility
    try:
        from app.services.trading_service import sync_open_trades
        db = SessionLocal()
        try:
            n = sync_open_trades(db)
            if n:
                logger.info(f"[LiveSync] Updated {n} trade(s)")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[LiveSync] Error: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _save_trade_record(db, analysis_record, execution: dict, trade_result: dict):
    """Persist a trade execution to TradeHistory."""
    try:
        trade = TradeHistory(
            symbol=execution.get("symbol", analysis_record.symbol),
            direction=execution.get("direction"),
            order_type=trade_result.get("order_type", "LIMIT"),
            quantity=trade_result.get("size", trade_result.get("quantity", 0)),
            entry_price=trade_result.get("entry_price"),
            stop_loss=execution.get("stop_loss"),
            take_profit=execution.get("tp1"),
            leverage=execution.get("recommended_leverage", 5),
            usdt_amount=trade_result.get("margin_usdt", 0),
            # For a paper pending order, trade_result is the order dict
            # (no entry_order_id/trade_id yet) — fall back to order_id so
            # _update_trade_record_on_fill/_on_cancel can find this row
            # later by entry_order_id (frozen schema, no dedicated column).
            entry_order_id=str(
                trade_result.get("entry_order_id")
                or trade_result.get("order_id")
                or trade_result.get("trade_id", "")
            ),
            sl_order_id=str(trade_result.get("sl_order_id", "")),
            tp_order_id=str(trade_result.get("tp1_order_id", "")),
            status=trade_result.get("status", "open"),
            analysis_id=analysis_record.id,
            notes=(
                f"mode={trade_result.get('mode', 'paper')} | "
                f"exchange={trade_result.get('exchange_id', settings.active_exchange)} | "
                f"confidence={execution.get('confidence')}% | "
                f"tp2={execution.get('tp2')} | "
                f"rr={execution.get('rr_ratio')}"
            ),
        )
        db.add(trade)
        db.commit()
        logger.debug(f"[Scheduler] Trade record saved: {trade.symbol} id={trade.id}")
    except Exception as e:
        logger.error(f"[Scheduler] Failed to save trade record: {e}")


def _update_trade_record_on_fill(order_id: str, position: dict) -> None:
    """
    Update the TradeHistory row for a pending order that just filled —
    looked up by entry_order_id, which _save_trade_record sets to the
    paper order_id at placement time (no dedicated column: the schema is
    frozen). Not every pending order has a row (only auto-executed daily-
    analysis signals call _save_trade_record today) — a missing row just
    logs and returns. A DB failure here must never abort pending-order
    processing, so it's caught and logged, not raised.
    """
    db = SessionLocal()
    try:
        trade = db.query(TradeHistory).filter_by(entry_order_id=str(order_id)).first()
        if not trade:
            logger.debug(f"[Paper] No TradeHistory row for filled order_id={order_id} (not tracked)")
            return
        trade.status = "open"
        trade.entry_price = position["entry_price"]
        trade.notes = f"{trade.notes or ''} | filled_at={position['fill_time']}"
        db.commit()
    except Exception as e:
        logger.error(f"[Paper] Failed to update trade record for filled order {order_id}: {e}")
    finally:
        db.close()


def _update_trade_record_on_cancel(order_id: str, reason: str) -> None:
    """
    Update the TradeHistory row for a pending order that was cancelled —
    same lookup as _update_trade_record_on_fill. pnl is set to 0.0 (never
    left NULL): trading_service.sync_open_trades treats
    status=='cancelled' AND pnl IS NULL as an unresolved live
    mis-classification to re-check against Binance, and a paper
    cancellation must not be picked up by that query.
    """
    db = SessionLocal()
    try:
        trade = db.query(TradeHistory).filter_by(entry_order_id=str(order_id)).first()
        if not trade:
            logger.debug(f"[Paper] No TradeHistory row for cancelled order_id={order_id} (not tracked)")
            return
        trade.status = "cancelled"
        trade.pnl = 0.0
        trade.notes = f"{trade.notes or ''} | cancelled: reason={reason}"
        db.commit()
    except Exception as e:
        logger.error(f"[Paper] Failed to update trade record for cancelled order {order_id}: {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler startup
# ─────────────────────────────────────────────────────────────────────────────

def start_scheduler():
    tz = pytz.timezone(settings.timezone)
    hour, minute = settings.daily_analysis_time.split(":")

    # Job 1: Daily HTF analysis
    scheduler.add_job(
        run_daily_analysis,
        trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
        id="daily_htf_analysis",
        replace_existing=True,
    )

    # Job 2: Swarm scan — every N minutes
    scheduler.add_job(
        run_swarm_scan,
        trigger="interval",
        minutes=settings.swarm_interval_minutes,
        id="swarm_scan",
        replace_existing=True,
    )

    # Job 3: Paper position updater — every 60 seconds
    if settings.is_paper_mode():
        scheduler.add_job(
            update_paper_positions,
            trigger="interval",
            seconds=60,
            id="paper_position_update",
            replace_existing=True,
        )

    # Job 4: Token list refresh — every 24 hours at midnight
    scheduler.add_job(
        refresh_token_list,
        trigger=CronTrigger(hour=0, minute=5, timezone=tz),
        id="token_refresh",
        replace_existing=True,
    )

    # Job 5: Live trade sync — every 60 seconds (live only)
    if settings.is_live_mode():
        scheduler.add_job(
            sync_live_trades,
            trigger="interval",
            seconds=60,
            id="live_trade_sync",
            replace_existing=True,
        )

    scheduler.start()

    mode_label = "📄 PAPER" if settings.is_paper_mode() else "🔴 LIVE"
    logger.info(
        f"[Scheduler] Started — mode={mode_label} | exchange={settings.active_exchange} | "
        f"daily={settings.daily_analysis_time} {settings.timezone} | "
        f"swarm=every {settings.swarm_interval_minutes}min"
    )
