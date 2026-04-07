"""
Fetches OHLCV candles and market data from Binance via ccxt.
Falls back to public endpoints so the app works without API keys for read-only data.
"""
import ccxt
import requests
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional
from app.config import settings

logger = logging.getLogger(__name__)

# CoinGecko IDs for fallback price data
COINGECKO_IDS = {
    "BTC/USDT": "bitcoin",
    "ETH/USDT": "ethereum",
    "SOL/USDT": "solana",
    "BNB/USDT": "binancecoin",
    "XRP/USDT": "ripple",
}


def _get_exchange() -> ccxt.binance:
    params: Dict = {
        "enableRateLimit": True,
        "options": {"defaultType": "future"},   # futures market by default
    }
    if settings.binance_api_key:
        params["apiKey"] = settings.binance_api_key
        params["secret"] = settings.binance_api_secret
    if settings.binance_testnet:
        params["urls"] = {
            "api": {
                "public": "https://testnet.binancefuture.com",
                "private": "https://testnet.binancefuture.com",
            }
        }
    return ccxt.binance(params)


def fetch_ohlcv(symbol: str, timeframe: str = "1d", limit: int = 100) -> List[Dict]:
    """Return list of OHLCV dicts sorted oldest→newest."""
    exchange = _get_exchange()
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    candles = []
    for o in raw:
        candles.append({
            "timestamp": datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).isoformat(),
            "open": o[1],
            "high": o[2],
            "low": o[3],
            "close": o[4],
            "volume": o[5],
        })
    return candles


def fetch_funding_rate(symbol: str) -> Optional[float]:
    """Current perpetual funding rate. Returns None if unavailable."""
    try:
        exchange = _get_exchange()
        # ccxt symbol format: BTC/USDT → BTCUSDT for futures endpoint
        data = exchange.fetch_funding_rate(symbol)
        return data.get("fundingRate")
    except Exception:
        return None


def fetch_fear_greed() -> Optional[int]:
    """Alternative.me Fear & Greed index (0-100). Public, no key needed."""
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        resp.raise_for_status()
        return int(resp.json()["data"][0]["value"])
    except Exception:
        return None


def fetch_ticker(symbol: str) -> Dict:
    """Current price + 24h stats. Tries Binance futures → spot → CoinGecko."""
    binance_symbol = symbol.replace("/", "")

    # 1. Binance futures
    try:
        url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={binance_symbol}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        t = resp.json()
        logger.info(f"Ticker from Binance futures: {symbol}")
        return {
            "symbol": symbol,
            "last": float(t.get("lastPrice", 0)),
            "high": float(t.get("highPrice", 0)),
            "low": float(t.get("lowPrice", 0)),
            "volume": float(t.get("volume", 0)),
            "change_pct": float(t.get("priceChangePercent", 0)),
        }
    except Exception as e:
        logger.warning(f"Binance futures ticker failed for {symbol}: {e}")

    # 2. Binance spot
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={binance_symbol}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        t = resp.json()
        logger.info(f"Ticker from Binance spot: {symbol}")
        return {
            "symbol": symbol,
            "last": float(t.get("lastPrice", 0)),
            "high": float(t.get("highPrice", 0)),
            "low": float(t.get("lowPrice", 0)),
            "volume": float(t.get("volume", 0)),
            "change_pct": float(t.get("priceChangePercent", 0)),
        }
    except Exception as e:
        logger.warning(f"Binance spot ticker failed for {symbol}: {e}")

    # 3. CoinGecko fallback (no API key needed, works everywhere)
    cg_id = COINGECKO_IDS.get(symbol)
    if cg_id:
        try:
            url = f"https://api.coingecko.com/api/v3/coins/{cg_id}?localization=false&tickers=false&community_data=false&developer_data=false"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            md = data["market_data"]
            logger.info(f"Ticker from CoinGecko: {symbol}")
            return {
                "symbol": symbol,
                "last": md["current_price"]["usd"],
                "high": md["high_24h"]["usd"],
                "low": md["low_24h"]["usd"],
                "volume": md["total_volume"]["usd"],
                "change_pct": md["price_change_percentage_24h"],
            }
        except Exception as e:
            logger.warning(f"CoinGecko ticker failed for {symbol}: {e}")

    raise Exception(f"All ticker sources failed for {symbol}")


def fetch_market_snapshot(symbol: str) -> Dict:
    """Aggregates everything needed for the AI prompt in one call."""
    candles_1d = fetch_ohlcv(symbol, "1d", limit=30)
    candles_4h = fetch_ohlcv(symbol, "4h", limit=48)
    candles_1h = fetch_ohlcv(symbol, "1h", limit=24)
    ticker = fetch_ticker(symbol)
    funding = fetch_funding_rate(symbol)
    fg = fetch_fear_greed()

    return {
        "symbol": symbol,
        "ticker": ticker,
        "funding_rate": funding,
        "fear_greed_index": fg,
        "candles_1d": candles_1d,
        "candles_4h": candles_4h,
        "candles_1h": candles_1h,
    }
