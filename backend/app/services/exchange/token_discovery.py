"""
Token Discovery — fetches Top 50 market cap tokens dynamically.

Sources:
  - CoinGecko free API (no auth required, 30 req/min limit)
  - Cross-checks availability on target exchange via ccxt

Updated daily via scheduler. Cached in-memory between refreshes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Filter sets
# ─────────────────────────────────────────────────────────────────────────────

STABLECOIN_COINGECKO_IDS = {
    "tether", "usd-coin", "dai", "binance-usd", "trueusd", "frax",
    "usdd", "fdusd", "paypal-usd", "first-digital-usd", "ethena-usde",
    "usual-usd", "mountain-protocol-usdm", "ageur", "husd", "usdp",
    "neutrino", "usdx-money", "nusd", "stasis-eurs", "reserve-rights-token",
    "celo-dollar", "terrausd", "magic-internet-money", "alchemix-usd",
}

WRAPPED_COINGECKO_IDS = {
    "wrapped-bitcoin", "wrapped-ether", "wrapped-steth", "wrapped-eeth",
    "staked-ether", "rocket-pool-eth", "coinbase-wrapped-staked-eth",
    "mantle-staked-ether", "ankr-staked-eth",
}

# Symbols to always skip regardless of market cap
SKIP_SYMBOLS = {
    "WBTC", "WETH", "WBNB", "WMATIC", "WAVAX",
    "STETH", "RETH", "CBETH", "METH",
}

# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

_cache: dict = {
    "symbols": [],             # list of "BTC/USDT" style symbols
    "raw_coins": [],           # raw CoinGecko response
    "last_updated": 0.0,
    "exchange_id": "",
}

CACHE_TTL_SECONDS = 86400   # 24 hours


# ─────────────────────────────────────────────────────────────────────────────
# CoinGecko fetch
# ─────────────────────────────────────────────────────────────────────────────

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

async def _fetch_coingecko_top(n: int = 60) -> list[dict]:
    """Fetch top N coins by market cap from CoinGecko."""
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": n,
        "page": 1,
        "sparkline": "false",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(COINGECKO_URL, params=params)
            resp.raise_for_status()
            coins = resp.json()
            logger.info(f"[TokenDiscovery] CoinGecko returned {len(coins)} coins")
            return coins
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            logger.warning("[TokenDiscovery] CoinGecko rate limited — retry in 60s")
            await asyncio.sleep(60)
            return await _fetch_coingecko_top(n)
        logger.error(f"[TokenDiscovery] CoinGecko HTTP error: {e}")
        return []
    except Exception as e:
        logger.error(f"[TokenDiscovery] CoinGecko fetch failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Exchange availability check
# ─────────────────────────────────────────────────────────────────────────────

async def _get_exchange_futures_symbols(exchange_id: str) -> set[str]:
    """
    Return set of base assets available as USDT perpetual futures on the exchange.
    Example return: {"BTC", "ETH", "SOL", ...}
    """
    from app.services.exchange.factory import create_exchange
    try:
        async with create_exchange(exchange_id) as client:
            markets = await client.exchange.load_markets()

        futures = set()
        for symbol, market in markets.items():
            if not market.get("active", False):
                continue
            # Match USDT perpetual futures
            if (
                "USDT" in symbol
                and market.get("active", False)
                and market.get("quote") == "USDT"
                and market.get("type") in ("swap", "future", "linear")
            ):
                base = market.get("base", "").upper()
                if base:
                    futures.add(base)

        logger.info(f"[TokenDiscovery] {exchange_id}: {len(futures)} USDT perp futures available")
        return futures

    except Exception as e:
        logger.warning(f"[TokenDiscovery] Failed to load markets from {exchange_id} — using extended fallback: {e}")
        return {
            # Tier 1 — top 20 market cap
            "BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "DOGE", "TON",
            "ADA", "SHIB", "LINK", "HBAR", "AVAX", "SUI", "BCH",
            "LTC", "DOT", "NEAR", "UNI", "APT",
            # Tier 2 — rank 21-50
            "PEPE", "ICP", "ETC", "POL", "FET", "RENDER", "STX",
            "IMX", "MKR", "RUNE", "GRT", "ENS", "ARB", "OP",
            "INJ", "TIA", "ATOM", "FTM", "AAVE", "WLD",
            "FIL", "EGLD", "VET", "ALGO", "THETA",
            # Tier 3 — rank 51-100
            "SEI", "BLUR", "WIF", "FLOKI", "BONK", "JUP",
            "SAND", "MANA", "AXS", "EOS", "GALA", "CHZ",
            "APE", "KAVA", "ZEC", "DASH", "NEO", "FLOW",
            "ONE", "CRV", "SUSHI", "1INCH", "BAL", "COMP",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Main discovery function
# ─────────────────────────────────────────────────────────────────────────────

async def get_top50_symbols(
    exchange_id: str = "binance",
    force_refresh: bool = False,
) -> list[str]:
    """
    Return list of top 50 non-stable, non-wrapped token USDT pairs,
    filtered by availability on the target exchange.

    Example: ["BTC/USDT", "ETH/USDT", "BNB/USDT", ...]

    Args:
        exchange_id: Target exchange to check availability
        force_refresh: Bypass 24h cache

    Returns:
        List of USDT pair strings, max 50 items
    """
    # Check cache
    now = time.time()
    cache_valid = (
        _cache["symbols"]
        and _cache["exchange_id"] == exchange_id
        and (now - _cache["last_updated"]) < CACHE_TTL_SECONDS
        and not force_refresh
    )
    if cache_valid:
        logger.debug(f"[TokenDiscovery] Returning cached list ({len(_cache['symbols'])} symbols)")
        return _cache["symbols"]

    logger.info(f"[TokenDiscovery] Refreshing top 50 token list for {exchange_id}...")

    # Parallel fetch: CoinGecko + exchange markets
    coins, exchange_bases = await asyncio.gather(
        _fetch_coingecko_top(100),
        _get_exchange_futures_symbols(exchange_id),
    )

    if not coins:
        logger.warning("[TokenDiscovery] CoinGecko returned no data — using cache if available")
        return _cache["symbols"] or _get_hardcoded_fallback()

    symbols = []
    for coin in coins:
        coin_id = coin.get("id", "").lower()
        ticker = coin.get("symbol", "").upper()

        # Skip stablecoins
        if coin_id in STABLECOIN_COINGECKO_IDS:
            continue

        # Skip wrapped tokens
        if coin_id in WRAPPED_COINGECKO_IDS:
            continue

        # Skip manually excluded symbols
        if ticker in SKIP_SYMBOLS:
            continue

        # Must be available as futures on target exchange
        if ticker not in exchange_bases:
            logger.debug(f"[TokenDiscovery] {ticker} not available as futures on {exchange_id}")
            continue

        pair = f"{ticker}/USDT"
        symbols.append(pair)

        if len(symbols) >= 50:
            break

    logger.info(f"[TokenDiscovery] Found {len(symbols)} tradeable symbols on {exchange_id}")

    # Update cache
    _cache.update({
        "symbols": symbols,
        "raw_coins": coins,
        "last_updated": now,
        "exchange_id": exchange_id,
    })

    return symbols


def get_cached_symbols() -> list[str]:
    """Return cached symbol list without triggering a refresh."""
    return _cache.get("symbols", [])


def get_token_info(symbol: str) -> Optional[dict]:
    """Return CoinGecko metadata for a symbol (from cache)."""
    ticker = symbol.split("/")[0].upper()
    for coin in _cache.get("raw_coins", []):
        if coin.get("symbol", "").upper() == ticker:
            return {
                "name": coin.get("name"),
                "symbol": ticker,
                "market_cap": coin.get("market_cap"),
                "market_cap_rank": coin.get("market_cap_rank"),
                "price": coin.get("current_price"),
                "volume_24h": coin.get("total_volume"),
                "change_24h": coin.get("price_change_percentage_24h"),
            }
    return None


def _get_hardcoded_fallback() -> list[str]:
    """Emergency fallback: well-known top tokens available on most exchanges."""
    return [
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
        "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "LINK/USDT", "DOT/USDT",
        "MATIC/USDT", "UNI/USDT", "ATOM/USDT", "LTC/USDT", "ETC/USDT",
        "NEAR/USDT", "APT/USDT", "ARB/USDT", "OP/USDT", "INJ/USDT",
        "SUI/USDT", "TIA/USDT", "SEI/USDT", "PEPE/USDT", "WIF/USDT",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: map symbol → exchange-specific format
# ─────────────────────────────────────────────────────────────────────────────

def to_exchange_symbol(symbol: str, exchange_id: str) -> str:
    """
    Convert "BTC/USDT" → exchange-specific format.
    ccxt generally handles this, but some exchanges need adjustments.
    """
    if exchange_id == "okx":
        base = symbol.split("/")[0]
        return f"{base}-USDT-SWAP"
    return symbol  # ccxt handles the rest via defaultType
