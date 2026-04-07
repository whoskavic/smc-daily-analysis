"""
APScheduler: runs daily analysis at the configured time (default 08:00 WIB).
Start this alongside FastAPI via app/main.py.
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
from datetime import datetime, date

from app.config import settings
from app.services.binance_service import fetch_market_snapshot
from app.services.claude_service import run_analysis
from app.models.database import SessionLocal, DailyAnalysis

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def run_daily_analysis():
    """Fetch market data + run Claude analysis for every watched symbol."""
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
            )
            db.add(record)
            db.commit()
            logger.info(f"[Scheduler] Saved analysis for {symbol}: bias={result['bias']}")

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
        f"[Scheduler] Daily analysis scheduled at {settings.daily_analysis_time} {settings.timezone}"
    )
