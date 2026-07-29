"""
Strategy Five -- Order Execution
------------------------------------------------------------------
Places and confirms real MIS (intraday) orders via Angel One's SmartAPI,
using the same authenticated-request pattern already proven throughout
this project (angel_one_client.py's AngelOneClient + _request helper).

Endpoints (verified against Angel One's SmartAPI docs,
https://smartapi.angelbroking.com/docs/Orders):
  POST /rest/secure/angelbroking/order/v1/placeOrder
  GET  /rest/secure/angelbroking/order/v1/getOrderBook

This module is intentionally standalone -- it does not touch or import
anything from Strategy Four's code. It only reuses the read-only
AngelOneClient/_request helpers that every script in this project has
already used safely for login and market data.
"""

import time

PLACE_ORDER_PATH = "/rest/secure/angelbroking/order/v1/placeOrder"
ORDER_BOOK_PATH = "/rest/secure/angelbroking/order/v1/getOrderBook"

TERMINAL_STATUSES = {"complete", "rejected", "cancelled"}

# getOrderBook is rate-limited to 1 request/second on Angel One's side --
# and that budget is shared across EVERY bot logged into the same account
# (Strategy Four included), not just this one. A rate-limit error here is
# routine, not a real failure -- it must never be treated as "order status
# unknown, give up," because the order may already be live on the exchange.


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "exceeding access rate" in msg or "403" in msg or "access denied" in msg


def place_market_order(client, _request, tradingsymbol: str, symboltoken: str,
                        transaction_type: str, quantity: int, exchange: str = "NSE",
                        price: float = None):
    """transaction_type: 'SELL' (short entry / averaging leg) or 'BUY' (cover/exit).
    LIMIT order priced at `price` (current LTP, passed by the caller) when given --
    MARKET orders get rejected by the exchange on stocks under cautionary/
    surveillance listing (confirmed via AB4036 on JIOFIN, 2026-07-17), while a
    LIMIT order at the same price goes through fine. Falls back to a true
    MARKET order only if no price is supplied. MIS (INTRADAY) product type
    guarantees the broker's own auto-square-off as a final backstop.

    Returns the raw API response dict. Raises RuntimeError on an API-level
    failure (status: false) so a bad order never gets silently swallowed."""
    if price:
        ordertype, price_str = "LIMIT", f"{price:.2f}"
    else:
        ordertype, price_str = "MARKET", "0"
    body = {
        "variety": "NORMAL",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": ordertype,
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": price_str,
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
    }
    resp = _request("POST", PLACE_ORDER_PATH, client._headers(auth=True), body)
    if not resp.get("status"):
        raise RuntimeError(
            f"Order placement FAILED for {tradingsymbol} {transaction_type} "
            f"qty={quantity}: {resp.get('message')} (errorcode={resp.get('errorcode')})"
        )
    order_id = resp["data"]["orderid"]
    return order_id, resp


def get_order_book(client, _request):
    resp = _request("GET", ORDER_BOOK_PATH, client._headers(auth=True), None)
    if not resp.get("status"):
        raise RuntimeError(f"getOrderBook FAILED: {resp.get('message')}")
    return resp.get("data") or []


def find_order(order_book: list, order_id: str):
    for o in order_book:
        if o.get("orderid") == order_id:
            return o
    return None


def wait_for_fill(client, _request, order_id: str, timeout_seconds: int = 60,
                   poll_interval: float = 3.0):
    """Poll the order book until this order reaches a terminal state
    (complete / rejected / cancelled) or the timeout elapses.

    Returns (status, avg_price, filled_qty) on completion.
    Raises RuntimeError if the order is rejected/cancelled, or TimeoutError
    if it never reaches a terminal state in time (caller must decide how to
    handle a stuck order -- this module never assumes success).

    A rate-limit error while checking is treated as "try again" -- NOT as
    an order failure. The order was already placed on the exchange; losing
    track of its status due to a shared rate limit (e.g. Strategy Four
    polling the same account concurrently) must never be confused with the
    order itself failing."""
    elapsed = 0.0
    while elapsed < timeout_seconds:
        try:
            book = get_order_book(client, _request)
        except Exception as e:
            if _is_rate_limit_error(e):
                time.sleep(poll_interval)
                elapsed += poll_interval
                continue
            raise
        order = find_order(book, order_id)
        if order is not None:
            status = (order.get("status") or "").lower()
            if status in TERMINAL_STATUSES:
                if status == "complete":
                    avg_price = float(order.get("averageprice") or 0)
                    filled_qty = int(order.get("filledshares") or 0)
                    return status, avg_price, filled_qty
                raise RuntimeError(
                    f"Order {order_id} ended in status '{status}': "
                    f"{order.get('text') or order.get('message') or ''}"
                )
        time.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"Order {order_id} did not reach a terminal state within {timeout_seconds}s "
                        f"-- the order may still be live on the exchange even though its status "
                        f"could not be confirmed. Check getOrderBook manually.")


def place_and_confirm(client, _request, tradingsymbol: str, symboltoken: str,
                       transaction_type: str, quantity: int, exchange: str = "NSE",
                       fill_timeout: int = 60, price: float = None):
    """Convenience wrapper: place an order and block until it's confirmed
    filled. Returns (order_id, avg_fill_price, filled_qty). Pass `price`
    (current LTP) to place a LIMIT order instead of MARKET -- see
    place_market_order() for why.

    This is the ONLY function the main live loop should call for actual
    order execution -- it never returns without either a confirmed fill or
    a raised exception, so the caller never has to guess whether an order
    went through."""
    order_id, _ = place_market_order(client, _request, tradingsymbol, symboltoken,
                                      transaction_type, quantity, exchange, price=price)
    status, avg_price, filled_qty = wait_for_fill(client, _request, order_id, fill_timeout)
    return order_id, avg_price, filled_qty
