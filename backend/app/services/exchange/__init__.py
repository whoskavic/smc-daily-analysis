# app/services/exchange/__init__.py
"""
Exchange abstraction layer — multi-exchange unified interface.
"""
from .factory import create_exchange, get_supported_exchanges, ExchangeClient
from .executor import execute_signal, get_live_positions, get_account_balance
from .paper_wallet import (
    init_paper_wallet,
    get_wallet_state,
    get_open_positions,
    get_closed_trades,
    has_position,
    get_position_count,
    reset_wallet,
)
from .token_discovery import get_top50_symbols, get_cached_symbols, get_token_info

__all__ = [
    "create_exchange",
    "get_supported_exchanges",
    "ExchangeClient",
    "execute_signal",
    "get_live_positions",
    "get_account_balance",
    "init_paper_wallet",
    "get_wallet_state",
    "get_open_positions",
    "get_closed_trades",
    "has_position",
    "get_position_count",
    "reset_wallet",
    "get_top50_symbols",
    "get_cached_symbols",
    "get_token_info",
]
