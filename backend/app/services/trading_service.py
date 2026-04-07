"""
Binance Futures trading service using the official binance-connector-python SDK.
https://github.com/binance/binance-connector-python
"""
import logging
import time
from typing import Dict, List, Optional

from binance.um_futures import UMFutures
from app.config import settings

logger = logging.getLogger(__name__)


def _client() -> UMFutures:
    return UMFutures(
        key=settings.binance_api_key,
        secret=settings.binance_api_secret,
        base_url="https://fapi.binance.com",
    )


# ── Account ────────────────────────────────────────────────────────────────────

def get_account_balance() -> Dict:
    """Returns USDT balance available for futures trading."""
    data = _client().balance(recvWindow=6000)
    for asset in data:
        if asset.get("asset") == "USDT":
            return {
                "asset": "USDT",
                "wallet_balance": float(asset.get("balance", 0)),
                "available_balance": float(asset.get("availableBalance", 0)),
                "unrealized_pnl": float(asset.get("crossUnPnl", 0)),
            }
    return {}


def get_positions() -> List[Dict]:
    """Returns all open futures positions (non-zero size)."""
    data = _client().get_position_risk(recvWindow=6000)
    positions = []
    for p in data:
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        positions.append({
            "symbol": p["symbol"],
            "side": "LONG" if amt > 0 else "SHORT",
            "size": abs(amt),
            "entry_price": float(p.get("entryPrice", 0)),
            "mark_price": float(p.get("markPrice", 0)),
            "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
            "leverage": int(p.get("leverage", 1)),
            "liquidation_price": float(p.get("liquidationPrice", 0)),
            "margin_type": p.get("marginType", ""),
        })
    return positions


def get_open_orders(symbol: str) -> List[Dict]:
    binance_symbol = symbol.replace("/", "")
    return _client().get_orders(symbol=binance_symbol, recvWindow=6000)


# ── Order placement ────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> Dict:
    binance_symbol = symbol.replace("/", "")
    return _client().change_leverage(
        symbol=binance_symbol, leverage=leverage, recvWindow=6000
    )


def place_order(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "MARKET",
    price: Optional[float] = None,
    reduce_only: bool = False,
) -> Dict:
    binance_symbol = symbol.replace("/", "")
    params = dict(
        symbol=binance_symbol,
        side=side,
        type=order_type,
        quantity=quantity,
        recvWindow=6000,
    )
    if order_type == "LIMIT":
        if price is None:
            raise ValueError("price required for LIMIT order")
        params["price"] = price
        params["timeInForce"] = "GTC"
    if reduce_only:
        params["reduceOnly"] = "true"
    return _client().new_order(**params)


def place_stop_market(
    symbol: str,
    side: str,
    stop_price: float,
    quantity: float,
) -> Dict:
    binance_symbol = symbol.replace("/", "")
    return _client().new_order(
        symbol=binance_symbol,
        side=side,
        type="STOP_MARKET",
        stopPrice=round(stop_price, 2),
        quantity=quantity,
        reduceOnly="true",
        recvWindow=6000,
    )


def place_take_profit_market(
    symbol: str,
    side: str,
    stop_price: float,
    quantity: float,
) -> Dict:
    binance_symbol = symbol.replace("/", "")
    return _client().new_order(
        symbol=binance_symbol,
        side=side,
        type="TAKE_PROFIT_MARKET",
        stopPrice=round(stop_price, 2),
        quantity=quantity,
        reduceOnly="true",
        recvWindow=6000,
    )


def execute_trade_plan(
    symbol: str,
    direction: str,
    usdt_amount: float,
    entry_price: Optional[float],
    stop_loss: float,
    take_profit: float,
    leverage: int = 10,
) -> Dict:
    """
    Full trade execution:
    1. Set leverage
    2. Place entry order (market or limit)
    3. Place stop-loss (STOP_MARKET)
    4. Place take-profit (TAKE_PROFIT_MARKET)
    """
    binance_symbol = symbol.replace("/", "")
    entry_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"

    set_leverage(symbol, leverage)

    # Get reference price for quantity calculation
    if entry_price:
        ref_price = entry_price
    else:
        ticker = _client().ticker_price(symbol=binance_symbol)
        ref_price = float(ticker["price"])

    quantity = round((usdt_amount * leverage) / ref_price, 3)

    # Entry order
    if entry_price:
        entry_order = place_order(symbol, entry_side, quantity, "LIMIT", price=round(entry_price, 2))
        order_type_used = "LIMIT"
    else:
        entry_order = place_order(symbol, entry_side, quantity, "MARKET")
        order_type_used = "MARKET"

    logger.info(f"Entry order placed: {entry_order.get('orderId')}")

    sl_order = place_stop_market(symbol, close_side, stop_loss, quantity)
    logger.info(f"SL order placed: {sl_order.get('orderId')}")

    tp_order = place_take_profit_market(symbol, close_side, take_profit, quantity)
    logger.info(f"TP order placed: {tp_order.get('orderId')}")

    return {
        "symbol": symbol,
        "direction": direction,
        "order_type": order_type_used,
        "quantity": quantity,
        "entry_price": entry_price or ref_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "leverage": leverage,
        "usdt_amount": usdt_amount,
        "entry_order_id": entry_order.get("orderId"),
        "sl_order_id": sl_order.get("orderId"),
        "tp_order_id": tp_order.get("orderId"),
        "status": "executed",
    }
