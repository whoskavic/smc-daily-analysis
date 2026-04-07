"""
Binance Futures trading service using direct REST API calls with HMAC auth.
Handles order placement, positions, balance, and trade history.
"""
import hashlib
import hmac
import time
import requests
import logging
import urllib3
from typing import Dict, List, Optional
from urllib.parse import urlencode
from app.config import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

FAPI_BASE = "https://fapi.binance.com"


def _sign(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        settings.binance_api_secret.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": settings.binance_api_key}


def _get(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    resp = requests.get(f"{FAPI_BASE}{path}", params=params, headers=_headers(), timeout=10, verify=False)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    resp = requests.post(f"{FAPI_BASE}{path}", params=params, headers=_headers(), timeout=10, verify=False)
    resp.raise_for_status()
    return resp.json()


def _delete(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    resp = requests.delete(f"{FAPI_BASE}{path}", params=params, headers=_headers(), timeout=10, verify=False)
    resp.raise_for_status()
    return resp.json()


def _delete(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    resp = requests.delete(f"{FAPI_BASE}{path}", params=params, headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Account ────────────────────────────────────────────────────────────────────

def get_account_balance() -> Dict:
    """Returns USDT balance available for futures trading."""
    data = _get("/fapi/v2/account")
    for asset in data.get("assets", []):
        if asset["asset"] == "USDT":
            return {
                "asset": "USDT",
                "wallet_balance": float(asset["walletBalance"]),
                "available_balance": float(asset["availableBalance"]),
                "unrealized_pnl": float(asset["unrealizedProfit"]),
            }
    return {}


def get_positions() -> List[Dict]:
    """Returns all open futures positions (non-zero size)."""
    data = _get("/fapi/v2/positionRisk")
    positions = []
    for p in data:
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        entry = float(p.get("entryPrice", 0))
        mark = float(p.get("markPrice", 0))
        positions.append({
            "symbol": p["symbol"],
            "side": "LONG" if amt > 0 else "SHORT",
            "size": abs(amt),
            "entry_price": entry,
            "mark_price": mark,
            "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
            "leverage": int(p.get("leverage", 1)),
            "liquidation_price": float(p.get("liquidationPrice", 0)),
            "margin_type": p.get("marginType", ""),
        })
    return positions


def get_open_orders(symbol: str) -> List[Dict]:
    """Returns open orders for a symbol."""
    binance_symbol = symbol.replace("/", "")
    data = _get("/fapi/v1/openOrders", {"symbol": binance_symbol})
    return data


# ── Order placement ────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> Dict:
    binance_symbol = symbol.replace("/", "")
    return _post("/fapi/v1/leverage", {"symbol": binance_symbol, "leverage": leverage})


def place_order(
    symbol: str,
    side: str,           # "BUY" or "SELL"
    quantity: float,
    order_type: str = "MARKET",   # "MARKET" or "LIMIT"
    price: Optional[float] = None,
    reduce_only: bool = False,
) -> Dict:
    binance_symbol = symbol.replace("/", "")
    params = {
        "symbol": binance_symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
    }
    if order_type == "LIMIT":
        if price is None:
            raise ValueError("price required for LIMIT order")
        params["price"] = price
        params["timeInForce"] = "GTC"
    if reduce_only:
        params["reduceOnly"] = "true"
    return _post("/fapi/v1/order", params)


def place_stop_market(
    symbol: str,
    side: str,
    stop_price: float,
    quantity: float,
    reduce_only: bool = True,
) -> Dict:
    binance_symbol = symbol.replace("/", "")
    params = {
        "symbol": binance_symbol,
        "side": side,
        "type": "STOP_MARKET",
        "stopPrice": round(stop_price, 2),
        "quantity": quantity,
        "reduceOnly": "true" if reduce_only else "false",
    }
    return _post("/fapi/v1/order", params)


def place_take_profit_market(
    symbol: str,
    side: str,
    stop_price: float,
    quantity: float,
    reduce_only: bool = True,
) -> Dict:
    binance_symbol = symbol.replace("/", "")
    params = {
        "symbol": binance_symbol,
        "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": round(stop_price, 2),
        "quantity": quantity,
        "reduceOnly": "true" if reduce_only else "false",
    }
    return _post("/fapi/v1/order", params)


def execute_trade_plan(
    symbol: str,
    direction: str,      # "LONG" or "SHORT"
    usdt_amount: float,  # how much USDT to risk
    entry_price: Optional[float],   # None = market order
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
    Returns a summary dict of all placed orders.
    """
    binance_symbol = symbol.replace("/", "")
    entry_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"

    # Set leverage first
    set_leverage(symbol, leverage)

    # Calculate quantity from USDT amount
    # Use entry_price if limit, else fetch current mark price
    if entry_price:
        ref_price = entry_price
    else:
        ticker = requests.get(
            f"{FAPI_BASE}/fapi/v1/ticker/price?symbol={binance_symbol}", timeout=5
        ).json()
        ref_price = float(ticker["price"])

    quantity = round((usdt_amount * leverage) / ref_price, 3)

    # Step 1: Entry order
    if entry_price:
        entry_order = place_order(symbol, entry_side, quantity, "LIMIT", price=round(entry_price, 2))
        order_type_used = "LIMIT"
    else:
        entry_order = place_order(symbol, entry_side, quantity, "MARKET")
        order_type_used = "MARKET"

    logger.info(f"Entry order placed: {entry_order.get('orderId')}")

    # Step 2: Stop loss
    sl_order = place_stop_market(symbol, close_side, stop_loss, quantity)
    logger.info(f"SL order placed: {sl_order.get('orderId')}")

    # Step 3: Take profit
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
