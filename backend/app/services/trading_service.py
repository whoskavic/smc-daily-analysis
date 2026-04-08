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

FAPI_BASE = "https://fapi.binance.com"   # USDⓈ-M Futures


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
    resp = requests.get(f"{FAPI_BASE}{path}", params=params, headers=_headers(), verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    params["signature"] = _sign(query)
    resp = requests.post(f"{FAPI_BASE}{path}", params=params, headers=_headers(), verify=False, timeout=10)
    if not resp.ok:
        raise Exception(f"Binance {resp.status_code}: {resp.text}")
    return resp.json()


def _delete(path: str, params: dict = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    params["signature"] = _sign(query)
    resp = requests.delete(f"{FAPI_BASE}{path}", params=params, headers=_headers(), verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Account ────────────────────────────────────────────────────────────────────

def get_account_balance() -> Dict:
    """Portfolio Margin balance. Try multiple endpoints."""
    data = _get("/fapi/v2/balance")
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
    """Portfolio Margin UM Futures positions: GET /fapi/v2/positionRisk"""
    data = _get("/fapi/v2/positionRisk")
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
    return _get("/fapi/v1/openOrders", {"symbol": symbol.replace("/", "")})


# ── Order placement ────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> Dict:
    return _post("/fapi/v1/leverage", {"symbol": symbol.replace("/", ""), "leverage": leverage})


def is_hedge_mode() -> bool:
    """Returns True if the account is in Hedge Mode (dualSidePosition=true)."""
    try:
        data = _get("/fapi/v1/positionSide/dual")
        return data.get("dualSidePosition", False)
    except Exception:
        return False


def place_order(symbol, side, quantity, order_type="MARKET", price=None, position_side=None) -> Dict:
    params = {
        "symbol": symbol.replace("/", ""),
        "side": side,
        "type": order_type,
        "quantity": quantity,
    }
    if order_type == "LIMIT":
        params["price"] = price
        params["timeInForce"] = "GTC"
    if position_side:
        params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "false"
    return _post("/fapi/v1/order", params)


def _place_algo_conditional(symbol, side, order_type, stop_price, quantity, position_side=None) -> Dict:
    """
    Place a conditional order via the Algo Order API.
    Binance migrated STOP_MARKET / TAKE_PROFIT_MARKET out of /fapi/v1/order on Dec 9 2025.
    Endpoint: POST /fapi/v1/algoOrder
    Params:   orderType (not type), triggerPrice (not stopPrice)
    Response: algoId (not orderId) — normalised below for consistency.
    """
    params = {
        "symbol": symbol.replace("/", ""),
        "side": side,
        "algoType": "CONDITIONAL",
        "type": order_type,
        "quantity": quantity,
        "triggerPrice": round(stop_price, 2),
        "workingType": "CONTRACT_PRICE",
    }
    if position_side:
        params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
    resp = _post("/fapi/v1/algoOrder", params)
    # Algo orders return algoId — expose as orderId so callers stay consistent
    resp.setdefault("orderId", resp.get("algoId"))
    return resp


def place_stop_market(symbol, side, stop_price, quantity, position_side=None) -> Dict:
    return _place_algo_conditional(symbol, side, "STOP_MARKET", stop_price, quantity, position_side)


def place_take_profit_market(symbol, side, stop_price, quantity, position_side=None) -> Dict:
    return _place_algo_conditional(symbol, side, "TAKE_PROFIT_MARKET", stop_price, quantity, position_side)


def execute_trade_plan(symbol, direction, usdt_amount, entry_price, stop_loss, take_profit, leverage=10) -> Dict:
    import math
    entry_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"
    # Hedge Mode requires positionSide; One-way Mode uses reduceOnly
    hedge = is_hedge_mode()
    position_side = direction if hedge else None  # "LONG" or "SHORT"

    set_leverage(symbol, leverage)

    if entry_price:
        ref_price = entry_price
    else:
        ticker = requests.get(
            f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.replace('/','')}", verify=False, timeout=5
        ).json()
        ref_price = float(ticker["price"])

    # Binance minimum notional is $100 — ensure we meet it
    MIN_NOTIONAL = 100.0
    notional = usdt_amount * leverage
    if notional < MIN_NOTIONAL:
        usdt_amount = MIN_NOTIONAL / leverage
        notional = MIN_NOTIONAL
        logger.info(f"usdt_amount adjusted to {usdt_amount} to meet $100 min notional")

    # Ceil to 3 decimals so rounding never drops the notional below $100
    quantity = math.ceil((notional / ref_price) * 1000) / 1000

    if entry_price:
        entry_order = place_order(symbol, entry_side, quantity, "LIMIT", price=round(entry_price, 2), position_side=position_side)
        order_type_used = "LIMIT"
    else:
        entry_order = place_order(symbol, entry_side, quantity, "MARKET", position_side=position_side)
        order_type_used = "MARKET"

    sl_order = place_stop_market(symbol, close_side, stop_loss, quantity, position_side=position_side)
    tp_order = place_take_profit_market(symbol, close_side, take_profit, quantity, position_side=position_side)

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
