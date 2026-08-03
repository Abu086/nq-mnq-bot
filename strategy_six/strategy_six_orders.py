"""
Strategy Six -- Order Execution
------------------------------------------------------------------
Self-contained -- does not import from Strategy Four's or Strategy
Five's files, only the shared, generic angel_one_client.py.
"""

import time

PLACE_ORDER_PATH = "/rest/secure/angelbroking/order/v1/placeOrder"
ORDER_BOOK_PATH  = "/rest/secure/angelbroking/order/v1/getOrderBook"
POSITION_PATH    = "/rest/secure/angelbroking/order/v1/getPosition"

TERMINAL_STATUSES = {"complete", "rejected", "cancelled"}


def place_market_order(client, _request, tradingsymbol, symboltoken,
                        transaction_type, quantity, exchange, price):
    body = {
        "variety": "NORMAL",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": "LIMIT",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": f"{price:.2f}",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
        "scripconsent": "yes",
    }
    resp = _request("POST", PLACE_ORDER_PATH, client._headers(auth=True), body)
    if not resp.get("status"):
        raise RuntimeError(
            f"Order placement FAILED for {tradingsymbol} {transaction_type} "
            f"qty={quantity}: {resp.get('message')} (errorcode={resp.get('errorcode')})"
        )
    return resp["data"]["orderid"]


def get_order_book(client, _request):
    resp = _request("GET", ORDER_BOOK_PATH, client._headers(auth=True))
    if not resp.get("status"):
        raise RuntimeError(f"getOrderBook FAILED: {resp.get('message')}")
    return resp.get("data") or []


def find_order(order_book, order_id):
    for o in order_book:
        if o.get("orderid") == order_id:
            return o
    return None


def wait_for_fill(client, _request, order_id, timeout_seconds=60, poll_interval=3.0):
    elapsed = 0.0
    while elapsed < timeout_seconds:
        try:
            book = get_order_book(client, _request)
            order = find_order(book, order_id)
            if order is not None:
                status = (order.get("status") or "").lower()
                if status in TERMINAL_STATUSES:
                    avg_price = float(order.get("averageprice") or 0)
                    filled_qty = int(order.get("filledshares") or 0)
                    text = order.get("text") or ""
                    return status, avg_price, filled_qty, text
        except Exception:
            pass
        time.sleep(poll_interval)
        elapsed += poll_interval
    return "timeout", 0.0, 0, "order did not reach a terminal state in time"


def get_real_position(client, _request, tradingsymbol):
    try:
        resp = _request("GET", POSITION_PATH, client._headers(auth=True))
        if not resp.get("status"):
            return None
        for p in resp.get("data") or []:
            if p.get("tradingsymbol") == tradingsymbol:
                return p
    except Exception:
        return None
    return None
