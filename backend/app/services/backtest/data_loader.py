"""
Historical OHLCV loader for backtesting — Phase 4.

binance_service.fetch_ohlcv() has no `since`/pagination support, which
multi-month backtests need — and binance_service.py must not be modified.
This module owns its own ccxt exchange instance (same construction pattern
as binance_service._get_exchange()) purely for paginated historical pulls,
and caches results in the existing CandleCache table so repeat backtest
runs don't re-hit the exchange.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import ccxt

from app.config import settings
from app.models.database import SessionLocal, CandleCache

logger = logging.getLogger(__name__)

_TF_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

_MAX_BARS_PER_CALL = 1000  # ccxt/binance futures OHLCV page size cap


def _get_exchange() -> ccxt.binance:
    """Separate instance from binance_service._get_exchange() so that file
    stays untouched; construction mirrors it for consistent behavior."""
    params: Dict = {
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    }
    if settings.binance_api_key:
        params["apiKey"] = settings.binance_api_key
        params["secret"] = settings.binance_api_secret
    return ccxt.binance(params)


def _candle_to_dict(o: list) -> Dict:
    return {
        "timestamp": datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).isoformat(),
        "open": o[1], "high": o[2], "low": o[3], "close": o[4], "volume": o[5],
    }


def _load_from_cache(db, symbol: str, timeframe: str, since_ms: int, until_ms: int) -> List[Dict]:
    since_dt = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    until_dt = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    rows = (
        db.query(CandleCache)
        .filter(
            CandleCache.symbol == symbol,
            CandleCache.timeframe == timeframe,
            CandleCache.timestamp >= since_dt,
            CandleCache.timestamp <= until_dt,
        )
        .order_by(CandleCache.timestamp.asc())
        .all()
    )
    return [
        {
            "timestamp": r.timestamp.replace(tzinfo=timezone.utc).isoformat(),
            "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume,
        }
        for r in rows
    ]


def _save_to_cache(db, symbol: str, timeframe: str, candles: List[Dict]) -> None:
    existing = {
        r[0] for r in db.query(CandleCache.timestamp).filter(
            CandleCache.symbol == symbol, CandleCache.timeframe == timeframe
        ).all()
    }
    new_rows = []
    for c in candles:
        ts = datetime.fromisoformat(c["timestamp"]).replace(tzinfo=None)
        if ts in existing:
            continue
        new_rows.append(CandleCache(
            symbol=symbol, timeframe=timeframe, timestamp=ts,
            open=c["open"], high=c["high"], low=c["low"], close=c["close"], volume=c["volume"],
        ))
    if new_rows:
        db.bulk_save_objects(new_rows)
        db.commit()


def fetch_historical_ohlcv(
    symbol: str,
    timeframe: str,
    since: datetime,
    until: Optional[datetime] = None,
    use_cache: bool = True,
) -> List[Dict]:
    """
    Paginated historical OHLCV fetch, oldest→newest, cached in CandleCache.

    Args:
        symbol: e.g. "BTC/USDT"
        timeframe: "15m" | "1h" | "4h" | "1d"
        since: start datetime (UTC, tz-aware)
        until: end datetime (UTC, tz-aware), defaults to now
        use_cache: read/write CandleCache; set False to force a fresh pull
    """
    until = until or datetime.now(timezone.utc)
    since_ms = int(since.timestamp() * 1000)
    until_ms = int(until.timestamp() * 1000)
    tf_ms = _TF_MS.get(timeframe)
    if tf_ms is None:
        raise ValueError(f"Unsupported timeframe: {timeframe} (expected one of {list(_TF_MS)})")

    db = SessionLocal()
    try:
        if use_cache:
            cached = _load_from_cache(db, symbol, timeframe, since_ms, until_ms)
            expected_bars = max(1, (until_ms - since_ms) // tf_ms)
            if cached and len(cached) >= expected_bars * 0.95:
                logger.info(f"[Backtest] {symbol} {timeframe}: {len(cached)} candles from cache")
                return cached

        exchange = _get_exchange()
        all_candles: List[Dict] = []
        cursor = since_ms
        while cursor < until_ms:
            raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=_MAX_BARS_PER_CALL)
            if not raw:
                break
            batch = [_candle_to_dict(o) for o in raw if o[0] <= until_ms]
            all_candles.extend(batch)
            last_ts = raw[-1][0]
            if last_ts <= cursor:
                break  # exchange stopped advancing — avoid infinite loop
            cursor = last_ts + tf_ms
            if len(raw) < _MAX_BARS_PER_CALL:
                break  # reached the end of available history
            time.sleep(exchange.rateLimit / 1000)

        if use_cache and all_candles:
            _save_to_cache(db, symbol, timeframe, all_candles)

        logger.info(f"[Backtest] {symbol} {timeframe}: fetched {len(all_candles)} candles from exchange")
        return all_candles
    finally:
        db.close()
