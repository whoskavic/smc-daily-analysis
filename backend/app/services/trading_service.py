"""
Binance Futures trading service using direct REST API with HMAC-SHA256 signing.
"""
import hashlib
import hmac
import time
import requests
import logging
import urllib3
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlencode
from app.config import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

FAPI_BASE = "https://fapi.binance.com"   # USDⓈ-M Futures
SAPI_BASE = "https://api.binance.com"    # Spot / Margin


def _sign(query_string: str) -> str:
    return hmac.new(
        settings.binance_api_secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": settings.binance_api_key}


def _get(path: str, params: dict = None, base: str = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    params["signature"] = _sign(query)
    url = f"{base or FAPI_BASE}{path}"
    resp = requests.get(url, params=params, headers=_headers(), verify=False, timeout=10)
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


def get_all_open_orders() -> List[Dict]:
    """Return all open futures limit/stop orders across all symbols."""
    data = _get("/fapi/v1/openOrders")
    orders = []
    for o in data:
        orders.append({
            "symbol": o.get("symbol"),
            "order_id": o.get("orderId"),
            "side": o.get("side"),
            "type": o.get("type"),
            "price": float(o.get("price", 0)),
            "stop_price": float(o.get("stopPrice", 0)),
            "orig_qty": float(o.get("origQty", 0)),
            "executed_qty": float(o.get("executedQty", 0)),
            "status": o.get("status"),
            "position_side": o.get("positionSide", "BOTH"),
            "time": o.get("time"),
            "reduce_only": o.get("reduceOnly", False),
        })
    return orders


def get_spot_account() -> Dict:
    """Return non-zero Spot wallet balances with USD value via GET /api/v3/account."""
    data = _get("/api/v3/account", base=SAPI_BASE)

    # Fetch all ticker prices (public endpoint, no auth needed)
    try:
        price_resp = requests.get(
            f"{SAPI_BASE}/api/v3/ticker/price", verify=False, timeout=10
        )
        price_map = {t["symbol"]: float(t["price"]) for t in price_resp.json()}
    except Exception:
        price_map = {}

    # Stablecoins are 1:1 to USD
    STABLES = {"USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP"}

    balances = []
    for b in data.get("balances", []):
        total = float(b["free"]) + float(b["locked"])
        if total <= 0:
            continue
        asset = b["asset"]
        if asset in STABLES:
            usd_price = 1.0
        else:
            usd_price = price_map.get(f"{asset}USDT") or price_map.get(f"{asset}BUSD")
        balances.append({
            "asset": asset,
            "free": float(b["free"]),
            "locked": float(b["locked"]),
            "total": total,
            "usd_price": usd_price,
            "usd_value": round(total * usd_price, 2) if usd_price else None,
        })

    # Sort by USD value descending (unknown prices go last)
    balances.sort(key=lambda x: x["usd_value"] or 0, reverse=True)
    total_usd = sum(b["usd_value"] for b in balances if b["usd_value"] is not None)
    return {"balances": balances, "total_usd": round(total_usd, 2)}


def get_open_algo_orders(symbol: str) -> List[Dict]:
    """Conditional (SL/TP) algo orders still pending trigger."""
    try:
        data = _get("/fapi/v1/openAlgoOrders", {"symbol": symbol.replace("/", "")})
        return data if isinstance(data, list) else data.get("orders", [])
    except Exception:
        return []


def has_active_position_or_order(symbol: str) -> bool:
    """
    Returns True if the symbol already has:
    - an open futures position (positionAmt != 0), OR
    - any open regular order (LIMIT/MARKET pending fill), OR
    - any open algo order (conditional SL/TP not yet triggered)
    """
    binance_symbol = symbol.replace("/", "")
    # Check positions
    try:
        positions = _get("/fapi/v2/positionRisk", {"symbol": binance_symbol})
        for p in positions:
            if abs(float(p.get("positionAmt", 0))) > 0:
                return True
    except Exception:
        pass
    # Check regular open orders
    try:
        orders = _get("/fapi/v1/openOrders", {"symbol": binance_symbol})
        if orders:
            return True
    except Exception:
        pass
    # Check algo (conditional) open orders
    try:
        algo = get_open_algo_orders(symbol)
        if algo:
            return True
    except Exception:
        pass
    return False


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


# ── Trade sync ─────────────────────────────────────────────────────────────────

def _check_entry_filled(binance_sym: str, entry_order_id: str) -> Optional[str]:
    """
    Query Binance for the entry order status.
    Returns: "FILLED" | "CANCELED" | "NEW" | "PARTIALLY_FILLED" | None (on error)
    """
    try:
        info = _get("/fapi/v1/order", {"symbol": binance_sym, "orderId": entry_order_id})
        return info.get("status")
    except Exception as exc:
        logger.warning(f"[Sync] order status fetch failed for {entry_order_id}: {exc}")
        return None


def _get_close_fills(binance_sym: str, close_side: str, start_ms: int) -> List[Dict]:
    """
    Fetch user trades (fills) for the closing side since start_ms.
    close_side: "BUY" for SHORT trades, "SELL" for LONG trades.
    """
    try:
        params: dict = {"symbol": binance_sym, "limit": 100}
        if start_ms:
            params["startTime"] = start_ms
        trades = _get("/fapi/v1/userTrades", params)
        return [t for t in trades if t.get("side") == close_side]
    except Exception as exc:
        logger.warning(f"[Sync] userTrades fetch failed for {binance_sym}: {exc}")
        return []


def sync_open_trades(db) -> int:
    """
    Sync trade statuses from Binance for every 'open' or wrongly-cancelled trade.

    Decision tree per trade:
      1. Entry order CANCELED on Binance       → mark 'cancelled' (entry never filled)
      2. Entry order NEW / PARTIALLY_FILLED    → skip (still pending)
      3. Entry order FILLED + position exists  → skip (position still running)
      4. Entry order FILLED + position gone:
           close fills found  → mark 'closed' with actual PnL from userTrades
           no close fills     → skip (race condition, retry next cycle)

    Also re-checks 'cancelled' trades with pnl=null in case they were wrongly
    classified (e.g. due to a previous income-API failure).

    Returns number of trades updated.
    """
    from app.models.database import TradeHistory
    from sqlalchemy import or_

    # Check open trades AND cancelled-without-PnL (possible mis-classification)
    trades = (
        db.query(TradeHistory)
        .filter(
            or_(
                TradeHistory.status == "open",
                (TradeHistory.status == "cancelled") & (TradeHistory.pnl == None),  # noqa: E711
            )
        )
        .all()
    )
    if not trades:
        return 0

    updated = 0
    for trade in trades:
        try:
            binance_sym = trade.symbol.replace("/", "")
            start_ms = int(trade.executed_at.timestamp() * 1000) if trade.executed_at else None
            close_side = "BUY" if trade.direction == "SHORT" else "SELL"

            # ── 1. Check entry order status on Binance ────────────────────────
            entry_status = _check_entry_filled(binance_sym, str(trade.entry_order_id))

            if entry_status in ("NEW", "PARTIALLY_FILLED"):
                # Still waiting to fill — ensure DB says open and move on
                if trade.status != "open":
                    trade.status = "open"
                    db.commit()
                continue

            if entry_status == "CANCELED":
                # Entry was explicitly cancelled on Binance (not filled)
                if trade.status != "cancelled":
                    trade.status = "cancelled"
                    db.commit()
                    updated += 1
                    logger.info(f"[Sync] Trade {trade.id} {trade.symbol}: entry CANCELED → cancelled")
                continue

            # entry_status == "FILLED" (or None/unknown — treat as potentially filled)

            # ── 2. Position still open? ───────────────────────────────────────
            try:
                positions = _get("/fapi/v2/positionRisk", {"symbol": binance_sym})
                if any(abs(float(p.get("positionAmt", 0))) > 0 for p in positions):
                    if trade.status != "open":
                        trade.status = "open"
                        db.commit()
                    continue  # Position still running
            except Exception:
                pass

            # ── 3. Entry filled + no position: read actual close fills ─────────
            close_fills = _get_close_fills(binance_sym, close_side, start_ms)
            if not close_fills:
                # No closing fills yet — could be race condition; skip this cycle
                logger.debug(f"[Sync] Trade {trade.id}: no close fills yet, will retry")
                continue

            realized = sum(float(f.get("realizedPnl", 0)) for f in close_fills)
            total_qty = sum(float(f.get("qty", 0)) for f in close_fills)
            avg_close = (
                sum(float(f["price"]) * float(f["qty"]) for f in close_fills) / total_qty
                if total_qty > 0 else None
            )

            trade.status = "closed"
            trade.pnl = round(realized, 4)
            trade.close_price = round(avg_close, 2) if avg_close else (
                trade.take_profit if realized >= 0 else trade.stop_loss
            )
            trade.closed_at = datetime.utcnow()
            db.commit()
            updated += 1
            logger.info(
                f"[Sync] Trade {trade.id} {trade.symbol}: closed, "
                f"PnL={realized:+.4f} USDT @ {trade.close_price}"
            )

        except Exception as exc:
            logger.error(f"[Sync] Trade {trade.id}: unexpected error: {exc}", exc_info=True)

    return updated
