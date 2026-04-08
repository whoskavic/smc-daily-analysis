"""
APScheduler: runs daily analysis at 22:30 WIB (Asia/Jakarta) then
automatically places a trade with $10 USDT / 10x leverage if Claude
gives a LONG or SHORT signal with valid SL and TP.

Order logic:
  - No entry_price from Claude           → MARKET (enter now)
  - entry_price set, within 0.5% of market → MARKET (too close, don't miss it)
  - entry_price set, price hasn't reached it yet (>0.5% away) → LIMIT at entry
  - entry_price set, price already blew past entry → MARKET (catch it now)
"""
import requests
import urllib3
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
from datetime import datetime, date

from app.config import settings
from app.services.binance_service import fetch_market_snapshot
from app.services.claude_service import run_analysis
from app.models.database import SessionLocal, DailyAnalysis, TradeHistory

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

AUTO_TRADE_USDT    = 10     # fixed margin per trade
AUTO_TRADE_LEVERAGE = 10    # fixed leverage
NEAR_THRESHOLD     = 0.005  # 0.5 % — if price is this close to entry, use MARKET


def _current_price(symbol: str) -> float:
    resp = requests.get(
        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.replace('/', '')}",
        verify=False, timeout=5,
    )
    return float(resp.json()["price"])


def _decide_order(direction: str, entry: float, current: float):
    """
    Returns (order_type_label, entry_price_for_api).
    entry_price_for_api=None  → MARKET in execute_trade_plan.
    entry_price_for_api=float → LIMIT at that price.
    """
    diff = abs(current - entry) / current

    if diff <= NEAR_THRESHOLD:
        # Price is essentially at the entry zone → MARKET
        return "MARKET", None

    if direction == "LONG":
        if current > entry:
            # Price above entry: waiting for pullback → LIMIT below
            return "LIMIT", entry
        else:
            # Price already below entry (blew through it) → MARKET now
            return "MARKET", None
    else:  # SHORT
        if current < entry:
            # Price below entry: waiting for rally → LIMIT above
            return "LIMIT", entry
        else:
            # Price already above entry → MARKET now
            return "MARKET", None


async def _auto_trade(db, analysis_record: DailyAnalysis, result: dict):
    """Place entry + SL + TP orders automatically after analysis."""
    from app.services.trading_service import execute_trade_plan

    direction = result.get("trade_direction")
    sl        = result.get("trade_sl")
    tp        = result.get("trade_tp")
    entry     = result.get("trade_entry")   # may be None
    symbol    = result["symbol"]

    if direction not in ("LONG", "SHORT"):
        logger.info(f"[AutoTrade] {symbol}: direction={direction}, skipping.")
        return
    if not sl or not tp:
        logger.info(f"[AutoTrade] {symbol}: missing SL/TP, skipping.")
        return

    # Determine LIMIT vs MARKET
    if entry:
        try:
            current = _current_price(symbol)
        except Exception as e:
            logger.error(f"[AutoTrade] {symbol}: could not fetch price: {e}, defaulting to MARKET")
            current = None

        if current:
            order_label, order_entry = _decide_order(direction, entry, current)
        else:
            order_label, order_entry = "MARKET", None
    else:
        # Claude gave no specific entry → enter at market
        order_label, order_entry = "MARKET", None

    logger.info(
        f"[AutoTrade] {symbol}: {direction} {order_label} | "
        f"entry={order_entry or 'market'} SL={sl} TP={tp} "
        f"usdt={AUTO_TRADE_USDT} lev={AUTO_TRADE_LEVERAGE}x"
    )

    try:
        trade_result = execute_trade_plan(
            symbol=symbol,
            direction=direction,
            usdt_amount=AUTO_TRADE_USDT,
            entry_price=order_entry,
            stop_loss=sl,
            take_profit=tp,
            leverage=AUTO_TRADE_LEVERAGE,
        )

        # Persist to trade history
        trade = TradeHistory(
            symbol=symbol,
            direction=direction,
            order_type=trade_result["order_type"],
            quantity=trade_result["quantity"],
            entry_price=trade_result["entry_price"],
            stop_loss=sl,
            take_profit=tp,
            leverage=AUTO_TRADE_LEVERAGE,
            usdt_amount=AUTO_TRADE_USDT,
            entry_order_id=str(trade_result.get("entry_order_id", "")),
            sl_order_id=str(trade_result.get("sl_order_id", "")),
            tp_order_id=str(trade_result.get("tp_order_id", "")),
            status="open",
            analysis_id=analysis_record.id,
            notes=f"Auto-trade | analysis {analysis_record.analysis_date} | bias={result['bias']}",
        )
        db.add(trade)
        db.commit()
        logger.info(f"[AutoTrade] {symbol}: orders placed successfully. trade_id={trade.id}")

    except Exception as e:
        logger.error(f"[AutoTrade] {symbol}: order failed: {e}", exc_info=True)


async def run_daily_analysis():
    """Fetch market data → run Claude analysis → auto-trade for every watched symbol."""
    today = date.today().isoformat()
    logger.info(f"[Scheduler] Running daily analysis for {today}")

    for symbol in settings.watch_symbols:
        db = SessionLocal()
        try:
            # Skip if already ran today for this symbol
            existing = (
                db.query(DailyAnalysis)
                .filter_by(symbol=symbol, analysis_date=today)
                .first()
            )
            if existing:
                logger.info(f"[Scheduler] {symbol} already analyzed today, skipping.")
                continue

            logger.info(f"[Scheduler] Fetching snapshot for {symbol}...")
            snapshot = fetch_market_snapshot(symbol)

            logger.info(f"[Scheduler] Running Claude analysis for {symbol}...")
            result = run_analysis(snapshot)

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
                trade_direction=result.get("trade_direction"),
                trade_entry=result.get("trade_entry"),
                trade_sl=result.get("trade_sl"),
                trade_tp=result.get("trade_tp"),
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            logger.info(
                f"[Scheduler] Saved analysis for {symbol}: "
                f"bias={result['bias']} dir={result.get('trade_direction')}"
            )

            # Auto-place trade based on analysis
            await _auto_trade(db, record, result)

        except Exception as e:
            logger.error(f"[Scheduler] Error analyzing {symbol}: {e}", exc_info=True)
        finally:
            db.close()


def start_scheduler():
    hour, minute = settings.daily_analysis_time.split(":")
    tz = pytz.timezone(settings.timezone)

    scheduler.add_job(
        run_daily_analysis,
        trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
        id="daily_analysis",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        f"[Scheduler] Daily analysis + auto-trade scheduled at "
        f"{settings.daily_analysis_time} {settings.timezone}"
    )
