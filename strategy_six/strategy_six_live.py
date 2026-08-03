"""
Strategy Six -- Live Trading Bot
======================================================================
Late-session breakout: 5% move away from previous close, only looked
for AFTER 11:30 AM, ignoring any stock that already crossed that band
earlier (that's Strategy Five's job). Symmetric 1% stop / 1% target,
one trade per stock per day, no averaging. No new entries after 2:45 PM.
Orders priced 0.15% above LTP (BUY) / 0.15% below LTP (SELL).
"""

import os
import sys
import json
import time
import logging
import datetime

sys.path.insert(0, "/root/trading_bot")
from angel_one_client import AngelOneClient, _request
from shared_session import try_load_shared_session

import strategy_six_state as st
import strategy_six_orders as ords

EXCHANGE = "NSE"
EFFECTIVE_CAPITAL = 200_000.0
MOVE_PCT = 0.05
SL_PCT = 0.01
TARGET_PCT = 0.01

TRIGGER_AFTER = datetime.time(11, 30)
ENTRY_END     = datetime.time(14, 45)
SQUARE_OFF    = datetime.time(15, 15)
MARKET_OPEN   = datetime.time(9, 15)

POLL_INTERVAL_SECONDS = 15
QUOTE_CHUNK_SIZE = 40
SLEEP_BETWEEN_QUOTE_CHUNKS = 0.5
HEARTBEAT_INTERVAL_SECONDS = 60

EXIT_ORDER_MAX_RETRIES = 8
EXIT_ORDER_RETRY_DELAY_SECONDS = 10

QUOTE_PATH  = "/rest/secure/angelbroking/market/v1/quote"
CANDLE_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"

REQUIRED_ENV_VARS = ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"]

DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")

STATE_DIR = "/root/trading_bot/strategy_six/state"
TOKEN_CACHE_PATH = os.path.join(STATE_DIR, "symbol_tokens.json")
LOG_DIR = "/root/trading_bot/strategy_six/logs"
TICK_CACHE_PATH = "/root/trading_bot/tick_size_cache.json"

BLACKLIST = {"ADANIENT", "ADANIPORTS", "COALINDIA", "ONGC"}

NIFTY50_SYMBOLS_FULL = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "ITC", "INFY", "INDIGO",
    "JSWSTEEL", "JIOFIN", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TMPV", "TATASTEEL",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]
SYMBOLS = [s for s in NIFTY50_SYMBOLS_FULL if s not in BLACKLIST]

SEARCH_TERM_OVERRIDES = {
    "BAJAJ-AUTO": ["BAJAJAUTO", "BAJAJ AUTO", "BAJAJ"],
    "M&M": ["MAHINDRA", "M&M", "MAHINDRA & MAHINDRA"],
}


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = st.today_ist_str()
    log_path = os.path.join(LOG_DIR, f"{date_str}_six.log")
    logger = logging.getLogger("strategy_six")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s IST %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = setup_logging()
_TICK_MAP = {}


def load_tick_map():
    global _TICK_MAP
    try:
        with open(TICK_CACHE_PATH) as f:
            _TICK_MAP = json.load(f)
        log.info(f"Loaded tick sizes for {len(_TICK_MAP)} symbols")
    except Exception:
        _TICK_MAP = {}
        log.warning("No tick size cache found -- defaulting all symbols to Rs.0.05")


def round_to_tick(price, symbol=None):
    tick = _TICK_MAP.get(symbol, 0.05) if symbol else 0.05
    return round(round(price / tick) * tick, 2)


def check_env():
    missing = [v for v in REQUIRED_ENV_VARS if v not in os.environ or not os.environ[v]]
    if missing:
        log.error(f"Missing required env vars: {', '.join(missing)}. Aborting.")
        sys.exit(1)


def login_secondary() -> AngelOneClient:
    client = AngelOneClient()
    log.info("Waiting for Strategy Five's shared session (up to 5 min)...")
    if try_load_shared_session(client, wait_seconds=300, poll_interval=5):
        log.info("✅ Loaded shared session -- no separate login performed")
        return client
    log.warning("⚠️ Shared session not available after waiting -- falling back to a direct login")
    if not client.login():
        log.error("Login failed. Aborting -- no orders placed today.")
        sys.exit(1)
    log.info("✅ Angel One login successful (fallback, direct)")
    return client


def load_token_cache() -> dict:
    if os.path.exists(TOKEN_CACHE_PATH):
        with open(TOKEN_CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_token_cache(cache: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(TOKEN_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def get_symbol_token(client, base_symbol, cache):
    tradingsymbol = f"{base_symbol}-EQ"
    if base_symbol in cache:
        return cache[base_symbol], tradingsymbol
    terms = SEARCH_TERM_OVERRIDES.get(base_symbol, [base_symbol.replace("-", " ").replace("&", " ")])
    for term in terms:
        try:
            results = client.search_scrip(EXCHANGE, term)
        except Exception as e:
            log.warning(f"  search_scrip failed for '{term}': {e}")
            results = None
        if results:
            for r in results:
                if r.get("tradingsymbol") == tradingsymbol:
                    time.sleep(1.0)
                    return r.get("symboltoken"), tradingsymbol
        time.sleep(1.0)
    return None, tradingsymbol


def resolve_watchlist_tokens(client):
    cache = load_token_cache()
    resolved = {}
    for sym in SYMBOLS:
        token, tradingsymbol = get_symbol_token(client, sym, cache)
        if token is None:
            log.warning(f"Could not resolve token for {sym} -- excluding today.")
            continue
        if sym not in cache:
            cache[sym] = token
            save_token_cache(cache)
        resolved[sym] = {"token": token, "tradingsymbol": tradingsymbol}
    log.info(f"Resolved {len(resolved)}/{len(SYMBOLS)} watchlist symbols.")
    return resolved


def fetch_prev_close(client, token, max_retries=3):
    end = st.now_ist()
    start = end - datetime.timedelta(days=12)
    body = {
        "exchange": EXCHANGE, "symboltoken": token, "interval": "ONE_DAY",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M"),
    }
    today_str = st.today_ist_str()
    delay = 2
    for attempt in range(max_retries):
        try:
            resp = _request("POST", CANDLE_PATH, client._headers(auth=True), body)
            if resp.get("status"):
                candles = resp.get("data") or []
                past = [c for c in candles if not str(c[0]).startswith(today_str)]
                if past:
                    return float(past[-1][4])
                return None
            log.warning(f"  getCandleData error for token {token}: {resp.get('message')}")
        except Exception as e:
            log.warning(f"  getCandleData request failed for token {token}: {e}")
        time.sleep(delay)
        delay *= 2
    return None


def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def get_market_data(client, mode, tokens, max_retries=3):
    body = {"mode": mode, "exchangeTokens": {EXCHANGE: tokens}}
    delay = 2
    for attempt in range(max_retries):
        try:
            resp = _request("POST", QUOTE_PATH, client._headers(auth=True), body)
            if resp.get("status"):
                data = resp.get("data") or {}
                return data.get("fetched") or []
            log.warning(f"  getMarketData error: {resp.get('message')} (attempt {attempt + 1})")
        except Exception as e:
            log.warning(f"  getMarketData request failed: {e} (attempt {attempt + 1})")
        time.sleep(delay)
        delay *= 2
    return []


def fetch_all_quotes(client, tokens, mode="OHLC"):
    out = {}
    for chunk in chunked(tokens, QUOTE_CHUNK_SIZE):
        fetched = get_market_data(client, mode, chunk)
        for q in fetched:
            out[str(q.get("symbolToken"))] = q
        time.sleep(SLEEP_BETWEEN_QUOTE_CHUNKS)
    return out


def _priced(ltp, symbol, txn):
    return round_to_tick(ltp * (1.0015 if txn == "BUY" else 0.9985), symbol)


def place_and_verify_entry(client, symbol, tradingsymbol, token, qty, direction, ltp):
    txn = "BUY" if direction == "LONG" else "SELL"
    price = _priced(ltp, symbol, txn)
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would {txn} (entry) {qty} {tradingsymbol} @ {price}")
        return f"DRYRUN-{tradingsymbol}-{int(time.time())}", price, qty
    order_id = ords.place_market_order(client, _request, tradingsymbol, token, txn, qty, EXCHANGE, price)
    status, avg_price, filled_qty, text = ords.wait_for_fill(client, _request, order_id, timeout_seconds=60)
    if status == "complete" and avg_price > 0:
        return order_id, avg_price, filled_qty or qty
    log.error(f"  ⚠️ {symbol} entry NOT filled (status={status}: {text})")
    return order_id, None, 0


def place_and_verify_exit(client, symbol, tradingsymbol, token, qty, direction, ltp, exit_reason):
    txn = "SELL" if direction == "LONG" else "BUY"
    price = _priced(ltp, symbol, txn)
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would {txn} (exit, {exit_reason}) {qty} {tradingsymbol} @ {price}")
        return f"DRYRUN-{tradingsymbol}-{int(time.time())}", price

    delay = EXIT_ORDER_RETRY_DELAY_SECONDS
    order_id = None
    for attempt in range(1, EXIT_ORDER_MAX_RETRIES + 1):
        try:
            order_id = ords.place_market_order(client, _request, tradingsymbol, token, txn, qty, EXCHANGE, price)
            break
        except Exception as e:
            log.error(f"  Exit order attempt {attempt}/{EXIT_ORDER_MAX_RETRIES} FAILED for "
                      f"{tradingsymbol} ({exit_reason}): {e}")
            time.sleep(delay)
    if order_id is None:
        log.critical(f"  🚨 {symbol}: could not even PLACE an exit order after "
                     f"{EXIT_ORDER_MAX_RETRIES} attempts -- CHECK MANUALLY.")
        return None, None

    status, avg_price, filled_qty, text = ords.wait_for_fill(client, _request, order_id, timeout_seconds=60)
    if status == "complete" and avg_price > 0:
        return order_id, avg_price

    log.error(f"  ⚠️ {symbol} {exit_reason} order NOT confirmed filled "
              f"(status={status}: {text}) -- checking real position at broker...")
    for _attempt in range(8):
        time.sleep(15)
        real_pos = ords.get_real_position(client, _request, tradingsymbol)
        if real_pos is not None and int(float(real_pos.get("netqty") or 0)) == 0:
            buy_avg = float(real_pos.get("totalbuyavgprice") or 0)
            sell_avg = float(real_pos.get("totalsellavgprice") or 0)
            real_price = buy_avg if direction == "SHORT" else sell_avg
            log.info(f"  ✅ {symbol}: broker had already auto-squared-off the position "
                     f"(real buy_avg={buy_avg}, sell_avg={sell_avg})")
            return order_id, real_price
    log.critical(f"  🚨 {symbol}: exit order not confirmed AND position still appears open "
                 f"at the broker after 2 minutes -- CHECK MANUALLY.")
    return order_id, None


def run_trading_day():
    if DRY_RUN:
        log.warning("DRY_RUN is enabled -- no real orders will be placed today.")

    weekday = st.now_ist().weekday()
    if weekday >= 5:
        log.info(f"Today (IST) is a weekend (weekday={weekday}). Nothing to do. Exiting.")
        return

    check_env()
    load_tick_map()
    client = login_secondary()
    watchlist = resolve_watchlist_tokens(client)
    if not watchlist:
        log.error("No symbols resolved -- aborting today's run.")
        return
    token_to_symbol = {info["token"]: sym for sym, info in watchlist.items()}

    log.info("Fetching previous close for all watchlist symbols...")
    prev_close = {}
    for sym, info in watchlist.items():
        pc = fetch_prev_close(client, info["token"])
        if pc is not None:
            prev_close[sym] = pc
        else:
            log.warning(f"Could not get previous close for {sym} -- excluding today.")
    watchlist = {s: i for s, i in watchlist.items() if s in prev_close}
    all_tokens = [i["token"] for i in watchlist.values()]
    log.info(f"{len(watchlist)} symbols have a usable previous close. Proceeding.")

    state = st.load_today_state()
    if st.is_new_trading_day(state):
        log.info("Fresh state for a new trading day.")

    upper = {sym: prev_close[sym] * (1 + MOVE_PCT) for sym in watchlist}
    lower = {sym: prev_close[sym] * (1 - MOVE_PCT) for sym in watchlist}
    crossed_up_early = {}
    crossed_dn_early = {}
    skip_zero_qty = set()

    last_heartbeat = 0.0
    log.info(f"Watchlist ({len(watchlist)}): {sorted(watchlist.keys())}")
    log.info(f"Entry window: {TRIGGER_AFTER} - {ENTRY_END} IST | Square-off: {SQUARE_OFF} IST")
    log.info("Entering main loop.")

    while True:
        now = st.now_ist()
        now_t = now.time()

        if now_t < MARKET_OPEN:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            st.heartbeat(state)
            last_heartbeat = time.time()

        quotes = fetch_all_quotes(client, all_tokens, mode="OHLC")
        in_entry_window = TRIGGER_AFTER <= now_t <= ENTRY_END

        for token, q in quotes.items():
            sym = token_to_symbol.get(token)
            if sym is None:
                continue
            ltp = q.get("ltp") or 0
            if ltp <= 0:
                continue

            if now_t < TRIGGER_AFTER:
                if ltp >= upper[sym]:
                    crossed_up_early[sym] = True
                if ltp <= lower[sym]:
                    crossed_dn_early[sym] = True
                continue

            tradingsymbol = watchlist[sym]["tradingsymbol"]
            tok = watchlist[sym]["token"]

            open_pos = st.open_positions(state)
            if sym in open_pos:
                pos = open_pos[sym]
                direction = pos["direction"]
                hit_target = hit_stop = False
                if direction == "LONG":
                    hit_target = ltp >= pos["target"]
                    hit_stop   = ltp <= pos["stop"]
                else:
                    hit_target = ltp <= pos["target"]
                    hit_stop   = ltp >= pos["stop"]

                exit_reason = None
                if hit_target:
                    exit_reason = "TARGET"
                elif hit_stop:
                    exit_reason = "STOP LOSS"
                elif now_t >= SQUARE_OFF:
                    exit_reason = "SQUAREOFF"

                if exit_reason:
                    order_id, real_exit_price = place_and_verify_exit(
                        client, sym, tradingsymbol, tok, pos["qty"], direction, ltp, exit_reason)
                    if real_exit_price is not None:
                        st.mark_exit(state, sym, real_exit_price, order_id, exit_reason)
                        icon = "🎯" if exit_reason == "TARGET" else "🛑"
                        log.info(f"  {icon} {sym} {exit_reason} @ {real_exit_price:.2f} | "
                                 f"P&L: Rs.{state['positions'][sym]['pnl']:,.2f}")
                continue

            if not in_entry_window:
                continue
            if st.has_traded_today(state, sym):
                continue
            if sym in skip_zero_qty:
                continue

            triggered = False
            direction = None
            if not crossed_up_early.get(sym) and ltp >= upper[sym]:
                triggered, direction = True, "LONG"
            elif not crossed_dn_early.get(sym) and ltp <= lower[sym]:
                triggered, direction = True, "SHORT"

            if not triggered:
                continue

            qty = int(EFFECTIVE_CAPITAL // ltp) if ltp > 0 else 0
            if qty < 1:
                log.warning(f"{sym}: trigger fired but qty rounds to 0 at price {ltp:.2f} -- skipping today.")
                skip_zero_qty.add(sym)
                continue

            log.info(f"ENTRY TRIGGER {sym} {direction} qty={qty} @ ~{ltp:.2f}")
            try:
                order_id, fill_price, filled_qty = place_and_verify_entry(
                    client, sym, tradingsymbol, tok, qty, direction, ltp)
                if fill_price is None:
                    skip_zero_qty.add(sym)
                    continue
                if direction == "LONG":
                    stop   = round_to_tick(fill_price * (1 - SL_PCT), sym)
                    target = round_to_tick(fill_price * (1 + TARGET_PCT), sym)
                else:
                    stop   = round_to_tick(fill_price * (1 + SL_PCT), sym)
                    target = round_to_tick(fill_price * (1 - TARGET_PCT), sym)
                st.mark_entry(state, sym, direction, fill_price, filled_qty, order_id, stop, target)
                log.info(f"{sym} {direction}: filled qty={filled_qty} @ {fill_price:.2f} "
                         f"stop={stop} target={target} order_id={order_id}")
            except Exception as e:
                log.error(f"Entry order FAILED for {sym}: {e}")
                if "AB4036" in str(e):
                    skip_zero_qty.add(sym)
                    log.warning(f"{sym}: AB4036 (exchange cautionary listing) -- "
                                f"auto-skipping further attempts on {sym} today.")

        if now_t >= SQUARE_OFF:
            still_open = st.open_positions(state)
            if not still_open:
                log.info("No open positions at square-off time. Ending today's run.")
                break
            if now_t >= datetime.time(15, 20):
                log.critical(f"🚨 {len(still_open)} position(s) still open at 15:20 IST and "
                             f"could not be confirmed closed -- CHECK MANUALLY: {list(still_open.keys())}")
                break

        time.sleep(POLL_INTERVAL_SECONDS)

    final_state = st.load_today_state()
    closed = [p for p in final_state["positions"].values() if p["status"] == "closed"]
    total_pnl = round(sum(p["pnl"] or 0 for p in closed), 2)
    wins = sum(1 for p in closed if (p["pnl"] or 0) > 0)
    log.info(f"DAY SUMMARY: {len(closed)} trade(s), {wins} winner(s), total P&L = Rs {total_pnl}")


def emergency_squareoff_all(client):
    try:
        state = st.load_today_state()
        open_pos = st.open_positions(state)
        if not open_pos:
            return
        log.critical(f"EMERGENCY SQUARE-OFF: attempting to close {len(open_pos)} open position(s) "
                     f"after an unexpected crash.")
        cache = load_token_cache()
        for sym, pos in open_pos.items():
            token = cache.get(sym)
            if not token:
                log.critical(f"  No cached token for {sym} -- cannot auto-close. "
                             f"Relying on broker's MIS auto-square-off.")
                continue
            tradingsymbol = f"{sym}-EQ"
            txn = "SELL" if pos["direction"] == "LONG" else "BUY"
            try:
                order_id = ords.place_market_order(client, _request, tradingsymbol, token,
                                                     txn, pos["qty"], EXCHANGE, pos["entry_price"])
                status, avg_price, _, _ = ords.wait_for_fill(client, _request, order_id, timeout_seconds=60)
                exit_price = avg_price if status == "complete" else pos["entry_price"]
                st.mark_exit(state, sym, exit_price, order_id, "EMERGENCY_SQUAREOFF")
                log.critical(f"  {sym}: emergency-closed @ {exit_price}")
            except Exception as e:
                log.critical(f"  {sym}: emergency close FAILED: {e}. Relying on broker's auto-square-off.")
    except Exception as e:
        log.critical(f"emergency_squareoff_all itself failed: {e}. Relying entirely on broker's auto-square-off.")


def main():
    try:
        run_trading_day()
    except SystemExit:
        raise
    except Exception as e:
        log.critical(f"UNHANDLED EXCEPTION in main loop: {e}", exc_info=True)
        try:
            client = login_secondary()
            emergency_squareoff_all(client)
        except Exception as e2:
            log.critical(f"Emergency square-off itself could not run: {e2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
