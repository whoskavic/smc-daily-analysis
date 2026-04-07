"""
Binance Futures trading service using direct REST API with HMAC-SHA256 signing.
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

PAPI_BASE = "https://papi.binance.com"   # Portfolio Margin API


def _sign(query_string: str) -> str:
    return hmac.new(
        settings.binance_api_secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": settings.binance_api_key}


def _get(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    params["signature"] = _sign(query)
    resp = requests.get(f"{PAPI_BASE}{path}", params=params, headers=_headers(), verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    params["signature"] = _sign(query)
    resp = requests.post(f"{PAPI_BASE}{path}", params=params, headers=_headers(), verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _delete(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    params["signature"] = _sign(query)
    resp = requests.delete(f"{PAPI_BASE}{path}", params=params, headers=_headers(), verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Account ────────────────────────────────────────────────────────────────────

def get_account_balance() -> Dict:
    """Portfolio Margin balance: GET /papi/v1/balance"""
    data = _get("/papi/v1/balance")
    for asset in data:
        if asset.get("asset") == "USDT":
            return {
                "asset": "USDT",
                "wallet_balance": float(asset.get("totalWalletBalance", asset.get("balance", 0))),
                "available_balance": float(asset.get("availableBalance", 0)),
                "unrealized_pnl": float(asset.get("umUnrealizedPNL", 0)),
            }
    return {}


def get_positions() -> List[Dict]:
    """Portfolio Margin UM Futures positions: GET /papi/v1/um/positionRisk"""
    data = _get("/papi/v1/um/positionRisk")
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
    return _get("/papi/v1/um/openOrders", {"symbol": symbol.replace("/", "")})


# ── Order placement ────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> Dict:
    return _post("/papi/v1/um/leverage", {"symbol": symbol.replace("/", ""), "leverage": leverage})


def place_order(symbol, side, quantity, order_type="MARKET", price=None, reduce_only=False) -> Dict:
    params = {
        "symbol": symbol.replace("/", ""),
        "side": side,
        "type": order_type,
        "quantity": quantity,
    }
    if order_type == "LIMIT":
        params["price"] = price
        params["timeInForce"] = "GTC"
    if reduce_only:
        params["reduceOnly"] = "true"
    return _post("/papi/v1/um/order", params)


def place_stop_market(symbol, side, stop_price, quantity) -> Dict:
    return _post("/papi/v1/um/order", {
        "symbol": symbol.replace("/", ""),
        "side": side,
        "type": "STOP_MARKET",
        "stopPrice": round(stop_price, 2),
        "quantity": quantity,
        "reduceOnly": "true",
    })


def place_take_profit_market(symbol, side, stop_price, quantity) -> Dict:
    return _post("/papi/v1/um/order", {
        "symbol": symbol.replace("/", ""),
        "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": round(stop_price, 2),
        "quantity": quantity,
        "reduceOnly": "true",
    })


def execute_trade_plan(symbol, direction, usdt_amount, entry_price, stop_loss, take_profit, leverage=10) -> Dict:
    entry_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"

    set_leverage(symbol, leverage)

    if entry_price:
        ref_price = entry_price
    else:
        ticker = requests.get(
            f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.replace('/','')}", verify=False, timeout=5
        ).json()
        ref_price = float(ticker["price"])

    quantity = round((usdt_amount * leverage) / ref_price, 3)

    if entry_price:
        entry_order = place_order(symbol, entry_side, quantity, "LIMIT", price=round(entry_price, 2))
        order_type_used = "LIMIT"
    else:
        entry_order = place_order(symbol, entry_side, quantity, "MARKET")
        order_type_used = "MARKET"

    sl_order = place_stop_market(symbol, close_side, stop_loss, quantity)
    tp_order = place_take_profit_market(symbol, close_side, take_profit, quantity)

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
