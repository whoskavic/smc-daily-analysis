"""
Exchange Factory — ccxt unified layer.
Supports: Binance (USDT-M Futures), Bybit, OKX, MEXC.
All instances are async (ccxt.async_support).
"""
from __future__ import annotations

import ccxt.async_support as ccxt
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class ExchangeMeta:
    ccxt_class: type
    default_type: str          # "future" | "swap"
    min_notional: float        # minimum order value in USDT
    symbol_suffix: str         # ":USDT" for perp, "" for spot
    supports_reduce_only: bool
    tp_sl_native: bool         # exchange handles TP/SL natively (Bybit yes, Binance via algo)
    notes: str


EXCHANGE_META: dict[str, ExchangeMeta] = {
    "binance": ExchangeMeta(
        ccxt_class=ccxt.binanceusdm,
        default_type="future",
        min_notional=100.0,
        symbol_suffix="",
        supports_reduce_only=True,
        tp_sl_native=False,   # uses algo orders (migrated Dec 2025)
        notes="USDT-M Futures. Min notional $100. Algo orders for SL/TP.",
    ),
    "bybit": ExchangeMeta(
        ccxt_class=ccxt.bybit,
        default_type="swap",
        min_notional=1.0,
        symbol_suffix=":USDT",
        supports_reduce_only=True,
        tp_sl_native=True,    # native unified TP/SL in single order
        notes="USDT Perpetual. Min $1. Recommended primary exchange.",
    ),
    "okx": ExchangeMeta(
        ccxt_class=ccxt.okx,
        default_type="swap",
        min_notional=1.0,
        symbol_suffix="-USDT-SWAP",
        supports_reduce_only=True,
        tp_sl_native=True,
        notes="USDT/USDC Perpetual. Min $1. Algo + copy-trade API.",
    ),
    "mexc": ExchangeMeta(
        ccxt_class=ccxt.mexc,
        default_type="swap",
        min_notional=5.0,
        symbol_suffix="",
        supports_reduce_only=True,
        tp_sl_native=False,
        notes="USDT Perpetual. Good altcoin coverage.",
    ),
}


class ExchangeClient:
    """
    Thin wrapper around a ccxt async exchange instance.
    Exposes exchange + metadata so callers don't need to look up meta separately.
    """

    def __init__(self, exchange_id: str, exchange: ccxt.Exchange, meta: ExchangeMeta):
        self.id = exchange_id
        self.exchange = exchange
        self.meta = meta

    async def close(self):
        await self.exchange.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


def _load_credentials(exchange_id: str) -> dict:
    """Load API credentials from env/settings for a given exchange."""
    # Import here to avoid circular import at module load
    try:
        from app.config import settings
    except ImportError:
        return {}

    cred_map = {
        "binance": {
            "apiKey": getattr(settings, "binance_api_key", ""),
            "secret": getattr(settings, "binance_api_secret", ""),
        },
        "bybit": {
            "apiKey": getattr(settings, "bybit_api_key", ""),
            "secret": getattr(settings, "bybit_api_secret", ""),
        },
        "okx": {
            "apiKey": getattr(settings, "okx_api_key", ""),
            "secret": getattr(settings, "okx_api_secret", ""),
            "password": getattr(settings, "okx_passphrase", ""),  # OKX requires passphrase
        },
        "mexc": {
            "apiKey": getattr(settings, "mexc_api_key", ""),
            "secret": getattr(settings, "mexc_api_secret", ""),
        },
    }
    return cred_map.get(exchange_id, {})


def create_exchange(
    exchange_id: str,
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
    passphrase: Optional[str] = None,
    sandbox: bool = False,
) -> ExchangeClient:
    """
    Create and return an ExchangeClient.

    Args:
        exchange_id: "binance" | "bybit" | "okx" | "mexc"
        api_key: Override credentials (optional — falls back to config/env)
        secret: Override credentials (optional)
        passphrase: Required for OKX (optional for others)
        sandbox: Use testnet/sandbox environment if available

    Returns:
        ExchangeClient — contains .exchange (ccxt) and .meta (ExchangeMeta)
    """
    exchange_id = exchange_id.lower()
    if exchange_id not in EXCHANGE_META:
        raise ValueError(
            f"Unsupported exchange: '{exchange_id}'. "
            f"Available: {list(EXCHANGE_META.keys())}"
        )

    meta = EXCHANGE_META[exchange_id]
    creds = _load_credentials(exchange_id)

    # Override with explicit credentials if provided
    if api_key:
        creds["apiKey"] = api_key
    if secret:
        creds["secret"] = secret
    if passphrase:
        creds["password"] = passphrase

    init_params: dict = {
        **creds,
        "enableRateLimit": True,
        "options": {
            "defaultType": meta.default_type,
            "adjustForTimeDifference": True,
        },
    }

    # Exchange-specific options
    if exchange_id == "binance":
        init_params["options"]["warnOnFetchOpenOrdersWithoutSymbol"] = False

    if exchange_id == "okx":
        init_params["options"]["broker"] = ""  # no broker ID required

    if sandbox:
        init_params["sandbox"] = True  # ccxt testnet flag (supported by Binance, Bybit)

    exchange = meta.ccxt_class(init_params)
    logger.info(f"[ExchangeFactory] Created {exchange_id} client (sandbox={sandbox})")

    return ExchangeClient(exchange_id=exchange_id, exchange=exchange, meta=meta)


def get_supported_exchanges() -> list[str]:
    return list(EXCHANGE_META.keys())


def get_meta(exchange_id: str) -> ExchangeMeta:
    if exchange_id not in EXCHANGE_META:
        raise ValueError(f"Unknown exchange: {exchange_id}")
    return EXCHANGE_META[exchange_id]
