"""
Strategy Eight -- Live Trading Bot
======================================================================
RSI(2) mean-reversion + gap-fade + VWAP-timed entry, Nifty 50 only.
Backtested on ~6 months of real 15-min Angel One data: 80 trades,
65.0% win rate, Rs 9,759 net (unconstrained) / Rs 2,997 net (capital-
constrained to 1 position at a time on Rs 20,000 x 5x = Rs 100,000
buying power). Flips negative above ~0.12% slippage/leg -- this is a
thin edge, monitor real fills closely against these backtest numbers.

Filters (see chat / Excel "Filters Applied" for full detail):
  1. Prior trading day's daily RSI(2): LONG if <=10, SHORT if >95.
  2. Today's open must gap 0.3%-1.5% in the SAME direction as the RSI
     extreme (fading the move, not following it).
  3. No blind entry at the open -- wait intraday for price to deviate
     >=0.5% further from the running VWAP (computed from today's
     15-min candles, matching the backtest exactly) before entering.
  4. Entry must trigger by 11:45 IST or the symbol is skipped for the
     day.
  5. Fixed 1% stop-loss / 1% target from entry. Else EOD square-off.
  6. One trade per symbol per day. Full buying power per trade.

Order execution, state tracking, tick rounding, retry/verification
logic are copied from Strategy Six's proven live-trading pattern
(strategy_eight_orders.py / strategy_eight_state.py).
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

import strategy_eight_state as st
import strategy_eight_orders as ords

EXCHANGE = "NSE"
BUDGET = 20_000.0
LEVERAGE = 5
BUYING_POWER = BUDGET * LEVERAGE          # Rs 100,000 per trade

RSI_PERIOD = 2
RSI_LOW = 10          # LONG candidate if prior-day RSI(2) <= 10
RSI_HIGH_SHORT = 95   # SHORT candidate if prior-day RSI(2) > 95 (strict)
GAP_MIN, GAP_MAX = 0.3, 1.5     # % gap band, same direction as RSI extreme
VWAP_DEV_PCT = 0.5              # entry trigger: price this far past VWAP
SL_PCT = 0.01
TARGET_PCT = 0.01

DAILY_HISTORY_DAYS = 150        # calendar days of daily candles for RSI(2) warm-up
VWAP_POLL_INTERVAL_SECONDS = 60 # how often to re-fetch today's 15-min candles

ENTRY_CUTOFF  = datetime.time(11, 45)
SQUARE_OFF    = datetime.time(15, 15)
MARKET_OPEN   = datetime.time(9, 15)

POLL_INTERVAL_SECONDS = 15      # LTP/stop/target monitoring poll
QUOTE_CHUNK_SIZE = 40
SLEEP_BETWEEN_QUOTE_CHUNKS = 0.5
HEARTBEAT_INTERVAL_SECONDS = 60

EXIT_ORDER_MAX_RETRIES = 8
EXIT_ORDER_RETRY_DELAY_SECONDS = 10

QUOTE_PATH  = "/rest/secure/angelbroking/market/v1/quote"
CANDLE_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"

REQUIRED_ENV_VARS = ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"]

DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")

STATE_DIR = "/root/trading_bot/strategy_eight/state"
TOKEN_CACHE_PATH = os.path.join(STATE_DIR, "symbol_tokens.json")
LOG_DIR = "/root/trading_bot/strategy_eight/logs"
TICK_CACHE_PATH = "/root/trading_bot/tick_size_cache.json"

# Same blacklist as Strategy Six -- symbols that have caused rejected
# orders / exchange cautionary-listing issues in this project before.
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


def _ist_time_converter(secs):
    # logging's default %(asctime)s uses the SYSTEM clock (UTC on this VPS)
    # regardless of the hardcoded "IST" label in the format string below --
    # confirmed 2026-08-05 this silently mislabeled every log line by 5.5
    # hours and caused a lengthy false "hung process" investigation. This
    # converter shifts the timestamp by IST's +5:30 offset before formatting
    # so the printed time is actually correct, not just labeled correctly.
    return time.gmtime(secs + 19800)


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = st.today_ist_str()
    log_path = os.path.join(LOG_DIR, f"{date_str}_eight.log")
    logger = logging.getLogger("strategy_eight")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s IST %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = _ist_time_converter
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


def login_tertiary() -> AngelOneClient:
    """Strategy Eight is a tertiary consumer of the shared session (Five is
    primary, Six is secondary) -- never logs in independently unless the
    shared session genuinely isn't available, to avoid the session-conflict
    issue this project hit earlier."""
    client = AngelOneClient()
    log.info("Waiting for the shared Angel One session (up to 20 min)...")
    if try_load_shared_session(client, wait_seconds=1200, poll_interval=5):
        log.info("Loaded shared session -- no separate login performed")
        return client
    log.warning("Shared session not available after waiting -- falling back to a direct login")
    if not client.login():
        log.error("Login failed. Aborting -- no orders placed today.")
        sys.exit(1)
    log.info("Angel One login successful (fallback, direct)")
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


# ----------------------------------------------------------------------
# RSI(2) signal -- fetch daily candles, replicate the backtest's
# pandas ewm(alpha=1/2, adjust=False, min_periods=2) RSI in plain
# Python (no pandas dependency on the VPS). Alpha=0.5 decays so fast
# that ~150 calendar days (~100 trading days) of warm-up converges to
# the same value as the backtest's full 2-year history.
# ----------------------------------------------------------------------
def compute_rsi2(closes):
    if len(closes) < 3:
        return None
    alpha = 1.0 / RSI_PERIOD
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain, avg_loss = gains[0], losses[0]
    for i in range(1, len(gains)):
        avg_gain = alpha * gains[i] + (1 - alpha) * avg_gain
        avg_loss = alpha * losses[i] + (1 - alpha) * avg_loss
    if avg_loss <= 0 and avg_gain <= 0:
        return 50.0
    if avg_loss <= 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def fetch_daily_history(client, token, max_retries=3):
    end = st.now_ist()
    start = end - datetime.timedelta(days=DAILY_HISTORY_DAYS)
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
                # exclude today's still-forming candle, if the API returns one
                past = [c for c in candles if not str(c[0]).startswith(today_str)]
                closes = [float(c[4]) for c in past]
                return closes
            log.warning(f"  getCandleData (daily) error for token {token}: {resp.get('message')}")
        except Exception as e:
            log.warning(f"  getCandleData (daily) request failed for token {token}: {e}")
        time.sleep(delay)
        delay *= 2
    return []


def build_rsi_signals(client, watchlist):
    """Returns {symbol: {"direction": "LONG"/"SHORT", "prev_rsi2": x, "prev_close": y}}
    for symbols whose prior day's RSI(2) was an extreme. Called once before
    the market opens."""
    signals = {}
    for sym, info in watchlist.items():
        closes = fetch_daily_history(client, info["token"])
        time.sleep(1.0)   # getCandleData rate-limits hard on back-to-back calls (HTTP 403
                           # "exceeding access rate") -- confirmed 2026-08-05 first deploy,
                           # every symbol got rejected without this pacing.
        if len(closes) < 20:
            log.warning(f"{sym}: only {len(closes)} daily closes available -- skipping (insufficient RSI warm-up).")
            continue
        prev_rsi2 = compute_rsi2(closes)
        prev_close = closes[-1]
        if prev_rsi2 is None or prev_close <= 0:
            continue
        if prev_rsi2 <= RSI_LOW:
            signals[sym] = {"direction": "LONG", "prev_rsi2": round(prev_rsi2, 1), "prev_close": prev_close}
        elif prev_rsi2 > RSI_HIGH_SHORT:
            signals[sym] = {"direction": "SHORT", "prev_rsi2": round(prev_rsi2, 1), "prev_close": prev_close}
    log.info(f"RSI(2) candidates today: {len(signals)} -- "
             f"{ {s: (v['direction'], v['prev_rsi2']) for s, v in signals.items()} }")
    return signals


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


def fetch_today_vwap_and_ltp(client, token, max_retries=2):
    """Fetch today's 15-min candles and compute VWAP exactly like the
    backtest: typical=(H+L+C)/3, running cumulative sum(typical*vol)/sum(vol).
    Returns (vwap, ltp) using the latest available candle's close as LTP
    proxy for this check (a live LTP quote is used separately for entry/exit
    pricing itself)."""
    now = st.now_ist()
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    body = {
        "exchange": EXCHANGE, "symboltoken": token, "interval": "FIFTEEN_MINUTE",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    delay = 2
    for attempt in range(max_retries):
        try:
            resp = _request("POST", CANDLE_PATH, client._headers(auth=True), body)
            if resp.get("status"):
                candles = resp.get("data") or []
                if len(candles) < 2:
                    return None, None
                sum_pv, sum_v = 0.0, 0.0
                for c in candles:
                    h, l, cl, v = float(c[2]), float(c[3]), float(c[4]), float(c[5])
                    typical = (h + l + cl) / 3.0
                    sum_pv += typical * v
                    sum_v += v
                if sum_v <= 0:
                    return None, None
                vwap = sum_pv / sum_v
                last_close = float(candles[-1][4])
                return vwap, last_close
            log.warning(f"  getCandleData (15min) error for token {token}: {resp.get('message')}")
        except Exception as e:
            log.warning(f"  getCandleData (15min) request failed for token {token}: {e}")
        time.sleep(delay)
    return None, None


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

    if status == "timeout":
        log.warning(f"  {symbol} entry order {order_id} still not terminal after 60s "
                    f"-- cancelling and re-checking real fill status...")
        ords.cancel_order(client, _request, order_id)
        time.sleep(3)
        status, avg_price, filled_qty, text = ords.wait_for_fill(
            client, _request, order_id, timeout_seconds=15, poll_interval=3.0)

    if status == "complete" and avg_price > 0:
        return order_id, avg_price, filled_qty or qty

    real_qty = 0
    real_price = None
    for _attempt in range(4):
        real_pos = ords.get_real_position(client, _request, tradingsymbol)
        net_qty = int(float(real_pos.get("netqty") or 0)) if real_pos else 0
        if net_qty != 0:
            real_qty = abs(net_qty)
            real_price = float(real_pos.get("totalbuyavgprice") or 0) if direction == "LONG" \
                         else float(real_pos.get("totalsellavgprice") or 0)
            break
        time.sleep(5)

    if real_qty > 0 and real_price:
        log.critical(f"  {symbol}: order reported {status} but a REAL position exists "
                     f"at the broker (qty={real_qty} @ {real_price:.2f}) -- treating as filled.")
        return order_id, real_price, real_qty

    log.error(f"  {symbol} entry NOT filled (status={status}: {text}), confirmed no real "
              f"position at broker.")
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
        log.critical(f"  {symbol}: could not even PLACE an exit order after "
                     f"{EXIT_ORDER_MAX_RETRIES} attempts -- CHECK MANUALLY.")
        return None, None

    status, avg_price, filled_qty, text = ords.wait_for_fill(client, _request, order_id, timeout_seconds=60)
    if status == "complete" and avg_price > 0:
        return order_id, avg_price

    log.error(f"  {symbol} {exit_reason} order NOT confirmed filled "
              f"(status={status}: {text}) -- checking real position at broker...")
    for _attempt in range(8):
        time.sleep(15)
        real_pos = ords.get_real_position(client, _request, tradingsymbol)
        if real_pos is not None and int(float(real_pos.get("netqty") or 0)) == 0:
            buy_avg = float(real_pos.get("totalbuyavgprice") or 0)
            sell_avg = float(real_pos.get("totalsellavgprice") or 0)
            real_price = buy_avg if direction == "SHORT" else sell_avg
            log.info(f"  {symbol}: broker had already auto-squared-off the position "
                     f"(real buy_avg={buy_avg}, sell_avg={sell_avg})")
            return order_id, real_price
    log.critical(f"  {symbol}: exit order not confirmed AND position still appears open "
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
    client = login_tertiary()
    watchlist = resolve_watchlist_tokens(client)
    if not watchlist:
        log.error("No symbols resolved -- aborting today's run.")
        return
    token_to_symbol = {info["token"]: sym for sym, info in watchlist.items()}

    log.info("Computing prior-day RSI(2) for all watchlist symbols...")
    rsi_signals = build_rsi_signals(client, watchlist)
    if not rsi_signals:
        log.info("No RSI(2) extreme signals today -- nothing to trade. Idling until square-off.")

    state = st.load_today_state()
    if st.is_new_trading_day(state):
        log.info("Fresh state for a new trading day.")

    # candidates still awaiting gap confirmation at the open
    pending_gap_check = dict(rsi_signals)
    # symbols confirmed (RSI + gap) and now being watched for VWAP entry
    watching = {}
    last_vwap_poll = {}
    vwap_fail_streak = {}
    day_open_captured = set()

    last_heartbeat = 0.0
    log.info(f"Entry cutoff: {ENTRY_CUTOFF} IST | Square-off: {SQUARE_OFF} IST")
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

        # ---- Step 1: capture today's open + apply gap filter, once per symbol ----
        if pending_gap_check:
            tokens = [watchlist[s]["token"] for s in pending_gap_check]
            quotes = fetch_all_quotes(client, tokens, mode="OHLC")
            still_pending = {}
            for sym, sig in pending_gap_check.items():
                tok = watchlist[sym]["token"]
                q = quotes.get(tok)
                day_open = q.get("open") if q else None
                if not day_open or float(day_open) <= 0:
                    still_pending[sym] = sig  # open not printed yet, retry
                    continue
                day_open = float(day_open)
                gap_pct = (day_open - sig["prev_close"]) / sig["prev_close"] * 100
                direction = sig["direction"]
                ok = False
                if direction == "LONG" and -GAP_MAX <= gap_pct <= -GAP_MIN:
                    ok = True
                elif direction == "SHORT" and GAP_MIN <= gap_pct <= GAP_MAX:
                    ok = True
                if ok:
                    watching[sym] = {"direction": direction, "prev_rsi2": sig["prev_rsi2"],
                                      "gap_pct": round(gap_pct, 2)}
                    log.info(f"{sym}: GAP CONFIRMED {direction} (RSI2={sig['prev_rsi2']}, gap={gap_pct:.2f}%) "
                             f"-- now watching for VWAP entry trigger.")
                else:
                    log.info(f"{sym}: gap {gap_pct:.2f}% outside [{GAP_MIN},{GAP_MAX}]% band for {direction} "
                             f"-- no trade today.")
            pending_gap_check = still_pending

        # ---- Step 2: monitor open positions for stop/target/EOD ----
        open_pos = st.open_positions(state)
        if open_pos:
            tokens = [watchlist[s]["token"] for s in open_pos if s in watchlist]
            quotes = fetch_all_quotes(client, tokens, mode="OHLC")
            for sym, pos in list(open_pos.items()):
                tok = watchlist[sym]["token"]
                q = quotes.get(tok)
                ltp = q.get("ltp") if q else None
                if not ltp or ltp <= 0:
                    continue
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
                    tradingsymbol = watchlist[sym]["tradingsymbol"]
                    order_id, real_exit_price = place_and_verify_exit(
                        client, sym, tradingsymbol, tok, pos["qty"], direction, ltp, exit_reason)
                    if real_exit_price is not None:
                        st.mark_exit(state, sym, real_exit_price, order_id, exit_reason)
                        icon = "TARGET" if exit_reason == "TARGET" else "STOP"
                        log.info(f"  {icon} {sym} {exit_reason} @ {real_exit_price:.2f} | "
                                 f"P&L: Rs.{state['positions'][sym]['pnl']:,.2f}")

        # ---- Step 3: VWAP-timed entry check for watched symbols ----
        if now_t <= ENTRY_CUTOFF:
            for sym in list(watching.keys()):
                if st.has_traded_today(state, sym):
                    del watching[sym]
                    continue
                if sym in st.open_positions(state):
                    continue
                last_poll = last_vwap_poll.get(sym, 0)
                streak = vwap_fail_streak.get(sym, 0)
                backoff = min(VWAP_POLL_INTERVAL_SECONDS * (2 ** streak), 600)  # exponential backoff on
                if time.time() - last_poll < backoff:                          # repeated rate-limit fails, capped 10 min
                    continue
                last_vwap_poll[sym] = time.time()

                tok = watchlist[sym]["token"]
                vwap, bar_close = fetch_today_vwap_and_ltp(client, tok, max_retries=1)
                time.sleep(1.0)   # same getCandleData rate-limit pacing as build_rsi_signals --
                                   # only matters if >1 symbol is being watched in the same cycle
                if vwap is None:
                    vwap_fail_streak[sym] = streak + 1
                    if vwap_fail_streak[sym] in (3, 6, 10, 15, 20):
                        log.warning(f"{sym}: VWAP fetch failed {vwap_fail_streak[sym]} consecutive "
                                    f"times (likely rate-limited) -- backing off to "
                                    f"{min(VWAP_POLL_INTERVAL_SECONDS * (2 ** vwap_fail_streak[sym]), 600)}s "
                                    f"between attempts.")
                    continue
                vwap_fail_streak[sym] = 0

                quotes = fetch_all_quotes(client, [tok], mode="OHLC")
                q = quotes.get(tok)
                ltp = q.get("ltp") if q else None
                if not ltp or ltp <= 0:
                    continue

                direction = watching[sym]["direction"]
                dev = (ltp - vwap) / vwap * 100
                triggered = (direction == "LONG" and dev <= -VWAP_DEV_PCT) or \
                            (direction == "SHORT" and dev >= VWAP_DEV_PCT)
                if not triggered:
                    continue

                qty = int(BUYING_POWER // ltp) if ltp > 0 else 0
                if qty < 1:
                    log.warning(f"{sym}: VWAP trigger fired but qty rounds to 0 at price {ltp:.2f} -- skipping.")
                    del watching[sym]
                    continue

                tradingsymbol = watchlist[sym]["tradingsymbol"]
                log.info(f"ENTRY TRIGGER {sym} {direction} qty={qty} @ ~{ltp:.2f} "
                         f"(VWAP={vwap:.2f}, dev={dev:.2f}%)")
                try:
                    order_id, fill_price, filled_qty = place_and_verify_entry(
                        client, sym, tradingsymbol, tok, qty, direction, ltp)
                    if fill_price is None:
                        del watching[sym]
                        continue
                    if direction == "LONG":
                        stop   = round_to_tick(fill_price * (1 - SL_PCT), sym)
                        target = round_to_tick(fill_price * (1 + TARGET_PCT), sym)
                    else:
                        stop   = round_to_tick(fill_price * (1 + SL_PCT), sym)
                        target = round_to_tick(fill_price * (1 - TARGET_PCT), sym)
                    st.mark_entry(state, sym, direction, fill_price, filled_qty, order_id, stop, target,
                                  prev_rsi2=watching[sym]["prev_rsi2"], gap_pct=watching[sym]["gap_pct"])
                    log.info(f"{sym} {direction}: filled qty={filled_qty} @ {fill_price:.2f} "
                             f"stop={stop} target={target} order_id={order_id}")
                except Exception as e:
                    log.error(f"Entry order FAILED for {sym}: {e}")
                    if "AB4036" in str(e):
                        log.warning(f"{sym}: AB4036 (exchange cautionary listing) -- skipping.")
                del watching[sym]
        else:
            if watching:
                log.info(f"Entry cutoff ({ENTRY_CUTOFF}) passed -- dropping {len(watching)} "
                         f"symbol(s) still waiting for VWAP trigger: {list(watching.keys())}")
                watching = {}

        # ---- Step 4: end-of-day check ----
        if now_t >= SQUARE_OFF:
            still_open = st.open_positions(state)
            if not still_open:
                log.info("No open positions at square-off time. Ending today's run.")
                break
            if now_t >= datetime.time(15, 20):
                log.critical(f"{len(still_open)} position(s) still open at 15:20 IST and "
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
            client = login_tertiary()
            emergency_squareoff_all(client)
        except Exception as e2:
            log.critical(f"Emergency square-off itself could not run: {e2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
