"""
Combined Strategy -- LIVE Trading Bot (real orders, real money)
====================================================================
REPLACES the short-only Strategy Five live bot in place. Runs BOTH sides
of the backtested combination in a single process, exactly as validated
in Nifty50_Combined_Strategy5_and_6.3_Backtest.xlsx (132 trades, zero
same-stock-same-day conflicts between the two sides):

  SHORT side (Strategy Five, live-chosen GAP-ONLY averaging mode) --
  unchanged from strategy_five_live.py:
    - Watchlist: Nifty 50 minus Strategy Five's OWN 8-stock blacklist
      (SHRIRAMFIN, TMPV, TRENT, EICHERMOT, POWERGRID, ADANIPORTS,
      ADANIENT, BAJFINANCE) -- 42 symbols.
    - Entry (09:15-11:30 IST): SHORT SELL when price is >=5% above
      previous close (gap-open or intraday touch).
    - Averaging (gap entries only): +3% further rise vs entry1 fires a
      2nd leg with equal buying power, blended average entry.
    - Exit: 2.5% HARD STOP above (blended) average entry, else EOD
      SQUARE-OFF at 15:15 IST. No profit target.

  BUY side (Strategy 6.3, No-Averaging variant -- the one used in the
  approved combined backtest, chosen for its better risk profile):
    - Watchlist: Nifty 50 minus Strategy 6.3's OWN 5-stock blacklist
      (TITAN, TRENT, HINDALCO, WIPRO, INFY) -- 45 symbols.
    - Entry (09:15-11:30 IST): BUY when price is <=5% below previous
      close (gap-down open or intraday touch).
    - No averaging -- single leg only, STOP LOSS active immediately at
      3% below entry1.
    - Exit: 3% STOP LOSS below entry, else EOD SQUARE-OFF at 15:15 IST.
      No profit target.

------------------------------------------------------------------
Blacklists are STRATEGY-SPECIFIC, never merged
------------------------------------------------------------------
A stock in Strategy Five's blacklist is excluded from the SHORT side
only -- it remains fully tradable on the BUY side, and vice versa. The
two watchlists are computed independently and only unioned for the
purpose of a single shared market-data fetch per cycle (see below).

------------------------------------------------------------------
Why one process, not two
------------------------------------------------------------------
Angel One's API rate limits are per-ACCOUNT, shared across every bot
logged in under the same client code (Strategy Four included -- this was
already root-caused earlier in this project). Running the SHORT and BUY
sides as two separate processes would double every quote/candle/order-book
call. Instead, this bot fetches ONE OHLC quote per symbol per cycle for
the union of both watchlists, and checks BOTH trigger conditions against
that same snapshot -- half the API load of running two bots.

Capital: Rs 40,000 x 5x MIS leverage = Rs 2,00,000 buying power PER LEG,
identical on both sides (matches every backtest variant). No artificial
capital split between the two sides -- per the user's explicit "one
shared capital pool" instruction, both sides draw against the same real
Angel One account balance, and the broker itself rejects an order if
there isn't enough margin. This bot does not attempt to model or enforce
a capital ceiling beyond that.

------------------------------------------------------------------
Indian Standard Time ONLY
------------------------------------------------------------------
Every timestamp this bot acts on comes from strategy_combined_state.now_ist(),
hardcoded to zoneinfo "Asia/Kolkata". This bot NEVER reads the VPS's
system clock/timezone or assumes anything about where the operator is
physically located.

------------------------------------------------------------------
Isolation from Strategy Four
------------------------------------------------------------------
This file, strategy_combined_state.py, and strategy_five_orders.py
(reused unchanged -- already direction-agnostic) are the only files this
bot touches. It only *reads* angel_one_client.py, and never touches any
of Strategy Four's files, state, or directories.

------------------------------------------------------------------
Relationship to strategy_five_live.py / strategy_five_state.py
------------------------------------------------------------------
Those files are NOT deleted or modified -- they remain on disk as a
reference/rollback copy of the short-only bot. This new file is what
strategy_five.service now points to (ExecStart updated), per the user's
explicit "replace in place" choice.

------------------------------------------------------------------
Safety notes (same guarantees as strategy_five_live.py)
------------------------------------------------------------------
- One trade per symbol PER DIRECTION per day (state-enforced) -- a
  symbol can legitimately trade on both sides same day if both extreme
  triggers fire (never observed in the 180-day backtest, but not
  structurally prevented).
- Every order goes through strategy_five_orders.place_and_confirm(),
  which blocks until the order is confirmed filled or raises.
- Exit orders (stop / EOD) are retried with backoff if they fail --
  leaving any position uncovered is the worst outcome.
- A DRY_RUN env var (default "false") logs every decision without
  placing real orders. Defaults to LIVE trading per explicit choice.
- State is atomically persisted after every state-changing action.
- If the whole process crashes unexpectedly, the top-level handler makes
  one best-effort attempt to square off every open position (both
  directions) before exiting.

Run (on VPS, in /root/trading_bot/strategy_five):
    set -a; source /root/trading_bot/.env; set +a
    python3 strategy_combined_live.py
"""

import os
import sys
import json
import time
import logging
import datetime
import signal


def _quote_fetch_alarm(signum, frame):
    raise TimeoutError("quote fetch exceeded timeout")


signal.signal(signal.SIGALRM, _quote_fetch_alarm)
QUOTE_FETCH_TIMEOUT_SEC = 30  # hard ceiling per fetch_all_quotes() call -- the
# underlying client has no socket timeout, and a hung read-only call here was
# the suspected cause of the 2026-07-16 ~34-min silent stall. Order placement
# calls are NOT wrapped -- only this read-only data fetch, to avoid any
# ambiguity about whether an order actually went through.

sys.path.insert(0, "/root/trading_bot")
from angel_one_client import AngelOneClient, _request  # noqa: E402

import strategy_combined_state as st
import strategy_five_orders as ords

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------
EXCHANGE = "NSE"
CAPITAL_PER_TRADE = 40000
LEVERAGE = 5
BUYING_POWER = CAPITAL_PER_TRADE * LEVERAGE      # per leg, both sides

TRIGGER_PCT = 5.0                # entry trigger, vs previous close, both sides
SHORT_AVERAGE_TRIGGER_PCT = 3.0  # SHORT leg2 trigger, vs leg1 entry (gap-only)
SHORT_HARD_STOP_PCT = 2.5        # SHORT risk level, vs (blended) average entry
SHORT_MAX_LEGS = 2

BUY_STOP_LOSS_PCT = 3.0          # BUY risk level, vs entry1 (no averaging, single leg only)

ENTRY_WINDOW_START = datetime.time(9, 15)
ENTRY_WINDOW_END = datetime.time(11, 30)
MARKET_OPEN = datetime.time(9, 15)
EOD_SQUAREOFF_TIME = datetime.time(15, 15)   # ahead of broker's own MIS auto-square-off
HARD_STOP_LOOP_END = datetime.time(15, 15)

POLL_INTERVAL_SECONDS = 15
HEARTBEAT_INTERVAL_SECONDS = 60
QUOTE_CHUNK_SIZE = 40
SLEEP_BETWEEN_QUOTE_CHUNKS = 0.5

LOGIN_MAX_RETRIES = 5
LOGIN_RETRY_DELAY_SECONDS = 30

EXIT_ORDER_MAX_RETRIES = 8
EXIT_ORDER_RETRY_DELAY_SECONDS = 10

QUOTE_PATH = "/rest/secure/angelbroking/market/v1/quote"
CANDLE_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"

REQUIRED_ENV_VARS = ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"]

DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")

STATE_DIR = "/root/trading_bot/strategy_five/state"
TOKEN_CACHE_PATH = os.path.join(STATE_DIR, "symbol_tokens.json")
LOG_DIR = "/root/trading_bot/strategy_five/logs"

# Strategy-specific blacklists -- NEVER merged. A stock excluded from one
# side remains fully tradable on the other.
SHORT_BLACKLIST = {"SHRIRAMFIN", "TMPV", "TRENT", "EICHERMOT", "POWERGRID",
                    "ADANIPORTS", "ADANIENT", "BAJFINANCE", "JIOFIN"}  # JIOFIN added 2026-07-17: broker rejects SHORT orders, AB4036 exchange cautionary listing
BUY_BLACKLIST = {"TITAN", "TRENT", "HINDALCO", "WIPRO", "INFY"}

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
SHORT_SYMBOLS = [s for s in NIFTY50_SYMBOLS_FULL if s not in SHORT_BLACKLIST]     # 42
BUY_SYMBOLS = [s for s in NIFTY50_SYMBOLS_FULL if s not in BUY_BLACKLIST]         # 45
# Union: every symbol tradable on at least one side. Only fetched/resolved once.
ALL_SYMBOLS = [s for s in NIFTY50_SYMBOLS_FULL
               if s in SHORT_BLACKLIST and s in BUY_BLACKLIST]
ALL_SYMBOLS = [s for s in NIFTY50_SYMBOLS_FULL if not (s in SHORT_BLACKLIST and s in BUY_BLACKLIST)]

SEARCH_TERM_OVERRIDES = {
    "BAJAJ-AUTO": ["BAJAJAUTO", "BAJAJ AUTO", "BAJAJ"],
    "M&M": ["MAHINDRA", "M&M", "MAHINDRA & MAHINDRA"],
}

# --------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------

def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = st.today_ist_str()
    log_path = os.path.join(LOG_DIR, f"{date_str}_combined.log")
    logger = logging.getLogger("strategy_combined")
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

# --------------------------------------------------------------------
# Startup checks
# --------------------------------------------------------------------

def check_env():
    missing = [v for v in REQUIRED_ENV_VARS if v not in os.environ or not os.environ[v]]
    if missing:
        log.error(f"Missing required env vars: {', '.join(missing)}. Aborting.")
        sys.exit(1)


def login_with_retries() -> AngelOneClient:
    client = AngelOneClient()
    for attempt in range(1, LOGIN_MAX_RETRIES + 1):
        try:
            if client.login():
                log.info("Login successful.")
                return client
            log.warning(f"Login attempt {attempt}/{LOGIN_MAX_RETRIES} returned falsy.")
        except Exception as e:
            log.warning(f"Login attempt {attempt}/{LOGIN_MAX_RETRIES} raised: {e}")
        time.sleep(LOGIN_RETRY_DELAY_SECONDS)
    log.error("All login attempts exhausted. Aborting -- no orders placed today.")
    sys.exit(1)


# --------------------------------------------------------------------
# Symbol token resolution (cached across days -- tokens are stable,
# shared cache file with the old short-only bot, safe to reuse)
# --------------------------------------------------------------------

def load_token_cache() -> dict:
    if os.path.exists(TOKEN_CACHE_PATH):
        with open(TOKEN_CACHE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_token_cache(cache: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(TOKEN_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


SEARCH_SCRIP_BASE_DELAY = 4.0
SEARCH_SCRIP_RATE_LIMIT_RETRIES = 4
SEARCH_SCRIP_RATE_LIMIT_BASE_BACKOFF = 8.0   # doubles each retry: 8, 16, 32, 64s


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "exceeding access rate" in msg or "403" in msg or "access denied" in msg


def get_symbol_token(client: AngelOneClient, base_symbol: str, cache: dict):
    tradingsymbol = f"{base_symbol}-EQ"
    if base_symbol in cache:
        return cache[base_symbol], tradingsymbol

    terms = SEARCH_TERM_OVERRIDES.get(
        base_symbol, [base_symbol.replace("-", " ").replace("&", " ")]
    )
    for term in terms:
        results = None
        for attempt in range(SEARCH_SCRIP_RATE_LIMIT_RETRIES + 1):
            try:
                results = client.search_scrip(EXCHANGE, term)
                break
            except Exception as e:
                if _is_rate_limit_error(e) and attempt < SEARCH_SCRIP_RATE_LIMIT_RETRIES:
                    backoff = SEARCH_SCRIP_RATE_LIMIT_BASE_BACKOFF * (2 ** attempt)
                    log.warning(f"  rate-limited on '{term}' (attempt {attempt + 1}) -- "
                                f"backing off {backoff:.0f}s")
                    time.sleep(backoff)
                    continue
                log.warning(f"  search_scrip failed for '{term}': {e}")
                results = None
                break
        if results:
            for r in results:
                if r.get("tradingsymbol") == tradingsymbol:
                    time.sleep(SEARCH_SCRIP_BASE_DELAY)
                    return r.get("symboltoken"), tradingsymbol
        time.sleep(SEARCH_SCRIP_BASE_DELAY)
    return None, tradingsymbol


def resolve_watchlist_tokens(client: AngelOneClient) -> dict:
    """Returns {base_symbol: {"token": ..., "tradingsymbol": ...}} for the
    UNION of both sides' watchlists. Cache saved after every new
    resolution so a rate-limit failure partway through never loses
    progress already made."""
    cache = load_token_cache()
    resolved = {}
    for sym in ALL_SYMBOLS:
        token, tradingsymbol = get_symbol_token(client, sym, cache)
        if token is None:
            log.warning(f"Could not resolve token for {sym} -- excluding from today's watchlist.")
            continue
        if sym not in cache:
            cache[sym] = token
            save_token_cache(cache)
        resolved[sym] = {"token": token, "tradingsymbol": tradingsymbol}
    log.info(f"Resolved {len(resolved)}/{len(ALL_SYMBOLS)} union watchlist symbols "
             f"(SHORT-side: {len(SHORT_SYMBOLS)}, BUY-side: {len(BUY_SYMBOLS)}).")
    return resolved


# --------------------------------------------------------------------
# Previous close (for both sides' 5% trigger thresholds)
# --------------------------------------------------------------------

def fetch_prev_close(client: AngelOneClient, token: str, max_retries: int = 3):
    """Last completed trading day's close, via a short ONE_DAY candle
    lookback -- naturally skips weekends/holidays."""
    end = st.now_ist()
    start = end - datetime.timedelta(days=12)
    body = {
        "exchange": EXCHANGE,
        "symboltoken": token,
        "interval": "ONE_DAY",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
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
                    return float(past[-1][4])   # candle = [ts, o, h, l, c, v]
                log.warning(f"No prior-day candle found for token {token}.")
                return None
            log.warning(f"  getCandleData error for token {token}: {resp.get('message')}")
        except Exception as e:
            log.warning(f"  getCandleData request failed for token {token}: {e}")
        time.sleep(delay)
        delay *= 2
    return None


# --------------------------------------------------------------------
# Live quotes (batched, single fetch per cycle for the union watchlist)
# --------------------------------------------------------------------

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def get_market_data(client: AngelOneClient, mode: str, tokens: list, max_retries: int = 3):
    body = {"mode": mode, "exchangeTokens": {EXCHANGE: tokens}}
    delay = 2
    for attempt in range(max_retries):
        try:
            resp = _request("POST", QUOTE_PATH, client._headers(auth=True), body)
            if resp.get("status"):
                data = resp.get("data") or {}
                unfetched = data.get("unfetched") or []
                if unfetched:
                    log.warning(f"  {len(unfetched)} tokens unfetched in this quote batch: "
                                f"{[u.get('symbolToken') for u in unfetched]}")
                return data.get("fetched") or []
            log.warning(f"  getMarketData error: {resp.get('message')} (attempt {attempt + 1})")
        except Exception as e:
            log.warning(f"  getMarketData request failed: {e} (attempt {attempt + 1})")
        time.sleep(delay)
        delay *= 2
    return []


def fetch_all_quotes(client: AngelOneClient, tokens: list, mode: str = "OHLC") -> dict:
    """Returns {token: quote_dict} across all chunks -- ONE fetch per
    cycle for the union watchlist, shared by both the SHORT and BUY
    trigger checks below."""
    out = {}
    for chunk in chunked(tokens, QUOTE_CHUNK_SIZE):
        fetched = get_market_data(client, mode, chunk)
        for q in fetched:
            out[str(q.get("symbolToken"))] = q
        time.sleep(SLEEP_BETWEEN_QUOTE_CHUNKS)
    return out


# --------------------------------------------------------------------
# Order helpers (wrap strategy_five_orders, honoring DRY_RUN)
# --------------------------------------------------------------------

def place_entry(client, tradingsymbol, token, qty, direction, tag="", price=None):
    """SHORT entry = SELL. BUY entry = BUY. Places a LIMIT order at `price`
    (current LTP) instead of MARKET -- MARKET orders get rejected by the
    exchange on stocks under cautionary/surveillance listing."""
    txn = "SELL" if direction == "SHORT" else "BUY"
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would {txn} (entry, {direction}) {qty} {tradingsymbol} {tag}")
        return f"DRYRUN-{tradingsymbol}-{int(time.time())}", 0.0, qty
    return ords.place_and_confirm(client, _request, tradingsymbol, token, txn, qty, EXCHANGE, price=price)


def place_exit_with_retries(client, tradingsymbol, token, qty, direction, tag="", price=None):
    """SHORT exit (cover) = BUY. BUY exit (close) = SELL. LIMIT order at
    `price` (current LTP) instead of MARKET, same reasoning as place_entry().
    Must not silently fail -- retries with backoff, because an uncovered
    position is the worst outcome on either side."""
    txn = "BUY" if direction == "SHORT" else "SELL"
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would {txn} (exit, {direction}) {qty} {tradingsymbol} {tag}")
        return f"DRYRUN-{tradingsymbol}-{int(time.time())}", 0.0, qty

    delay = EXIT_ORDER_RETRY_DELAY_SECONDS
    for attempt in range(1, EXIT_ORDER_MAX_RETRIES + 1):
        try:
            return ords.place_and_confirm(client, _request, tradingsymbol, token, txn, qty, EXCHANGE, price=price)
        except Exception as e:
            log.error(f"  Exit order attempt {attempt}/{EXIT_ORDER_MAX_RETRIES} FAILED for "
                      f"{tradingsymbol} ({direction}) qty={qty} ({tag}): {e}")
            time.sleep(delay)
    log.critical(f"  ALL exit-order attempts exhausted for {tradingsymbol} ({direction}) qty={qty} "
                 f"({tag}). Position remains open -- relying on broker's MIS auto-square-off.")
    return None, None, None


# --------------------------------------------------------------------
# Main trading day
# --------------------------------------------------------------------

def run_trading_day():
    if DRY_RUN:
        log.warning("DRY_RUN is enabled -- no real orders will be placed today.")

    weekday = st.now_ist().weekday()
    if weekday >= 5:
        log.info(f"Today (IST) is a weekend (weekday={weekday}). Nothing to do. Exiting.")
        return

    check_env()
    client = login_with_retries()
    watchlist = resolve_watchlist_tokens(client)
    if not watchlist:
        log.error("No symbols resolved -- aborting today's run.")
        return

    token_to_symbol = {info["token"]: sym for sym, info in watchlist.items()}

    log.info("Fetching previous close for all union-watchlist symbols...")
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

    short_watch = {s for s in watchlist if s in SHORT_SYMBOLS}
    buy_watch = {s for s in watchlist if s in BUY_SYMBOLS}
    log.info(f"SHORT-side watchlist: {len(short_watch)} symbols. "
             f"BUY-side watchlist: {len(buy_watch)} symbols.")

    state = st.load_today_state()
    if st.is_new_trading_day(state):
        log.info("Fresh state for a new trading day.")

    day_open = {}
    short_threshold = {sym: prev_close[sym] * (1 + TRIGGER_PCT / 100) for sym in watchlist}
    buy_threshold = {sym: prev_close[sym] * (1 - TRIGGER_PCT / 100) for sym in watchlist}
    is_gap_short = {}
    is_gap_buy = {}
    skip_zero_qty = set()   # (symbol, direction) pairs where qty would round to 0

    last_heartbeat = 0.0

    log.info(f"Union watchlist ({len(watchlist)}): {sorted(watchlist.keys())}")
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

        signal.alarm(QUOTE_FETCH_TIMEOUT_SEC)
        try:
            quotes = fetch_all_quotes(client, all_tokens, mode="OHLC")
        except Exception as e:
            log.error(f"Quote fetch failed this cycle: {e}")
            quotes = {}
        finally:
            signal.alarm(0)

        in_entry_window = ENTRY_WINDOW_START <= now_t <= ENTRY_WINDOW_END

        for token, q in quotes.items():
            sym = token_to_symbol.get(token)
            if sym is None:
                continue
            open_px = q.get("open") or 0
            ltp = q.get("ltp") or 0
            if open_px and sym not in day_open:
                day_open[sym] = open_px
                is_gap_short[sym] = open_px >= short_threshold[sym]
                is_gap_buy[sym] = open_px <= buy_threshold[sym]
                log.info(f"{sym}: day open={open_px:.2f} prev_close={prev_close[sym]:.2f} "
                         f"short_threshold={short_threshold[sym]:.2f} is_gap_short={is_gap_short[sym]} "
                         f"buy_threshold={buy_threshold[sym]:.2f} is_gap_buy={is_gap_buy[sym]}")

            if sym not in day_open:
                continue

            # ================= SHORT SIDE ENTRY =================
            if (in_entry_window and sym in short_watch
                    and not st.has_traded_today(state, sym, "SHORT")
                    and (sym, "SHORT") not in skip_zero_qty):

                triggered = False
                trigger_price = None
                if is_gap_short[sym]:
                    triggered = True
                    trigger_price = day_open[sym]
                elif ltp and ltp >= short_threshold[sym]:
                    triggered = True
                    trigger_price = short_threshold[sym]

                if triggered:
                    qty = int(BUYING_POWER // trigger_price) if trigger_price > 0 else 0
                    if qty < 1:
                        log.warning(f"{sym} SHORT: trigger fired but qty rounds to 0 at price "
                                    f"{trigger_price:.2f} -- skipping for today.")
                        skip_zero_qty.add((sym, "SHORT"))
                    else:
                        tradingsymbol = watchlist[sym]["tradingsymbol"]
                        tok = watchlist[sym]["token"]
                        kind = "GAP" if is_gap_short[sym] else "INTRADAY"
                        # Never sell below the trigger threshold -- guards the gap case,
                        # where ltp (from this same cycle's quote) can occasionally sit
                        # below short_threshold even though day_open already cleared it.
                        entry_price = short_threshold[sym] if not ltp else max(ltp, short_threshold[sym])
                        log.info(f"SHORT ENTRY TRIGGER {sym} ({kind}) qty={qty} @ ~{trigger_price:.2f} "
                                 f"order_price={entry_price:.2f}")
                        try:
                            order_id, fill_price, filled_qty = place_entry(
                                client, tradingsymbol, tok, qty, "SHORT", tag=f"entry1 {kind}", price=entry_price)
                            fill_price = fill_price or trigger_price
                            filled_qty = filled_qty or qty
                            risk_level = None if is_gap_short[sym] else fill_price * (1 + SHORT_HARD_STOP_PCT / 100)
                            st.mark_entry1(state, sym, "SHORT", is_gap_short[sym], fill_price,
                                            filled_qty, order_id, risk_level)
                            log.info(f"{sym} SHORT: Leg1 filled qty={filled_qty} @ {fill_price:.2f} "
                                     f"order_id={order_id} risk_level={risk_level}")
                        except TimeoutError as e:
                            log.critical(f"{sym} SHORT: entry order placed but fill status could not "
                                         f"be confirmed ({e}). May be LIVE and untracked -- check "
                                         f"getOrderBook manually. Not retriggering today.")
                            skip_zero_qty.add((sym, "SHORT"))
                        except Exception as e:
                            log.error(f"SHORT entry order FAILED for {sym}: {e} -- skipping trigger.")
                            if "AB4036" in str(e):
                                skip_zero_qty.add((sym, "SHORT"))
                                log.warning(f"{sym} SHORT: AB4036 (exchange cautionary listing) is a "
                                            f"permanent rejection -- auto-skipping further SHORT "
                                            f"attempts on {sym} for today.")

            # ================= BUY SIDE ENTRY =================
            if (in_entry_window and sym in buy_watch
                    and not st.has_traded_today(state, sym, "BUY")
                    and (sym, "BUY") not in skip_zero_qty):

                triggered = False
                trigger_price = None
                if is_gap_buy[sym]:
                    triggered = True
                    trigger_price = day_open[sym]
                elif ltp and ltp <= buy_threshold[sym]:
                    triggered = True
                    trigger_price = buy_threshold[sym]

                if triggered:
                    qty = int(BUYING_POWER // trigger_price) if trigger_price > 0 else 0
                    if qty < 1:
                        log.warning(f"{sym} BUY: trigger fired but qty rounds to 0 at price "
                                    f"{trigger_price:.2f} -- skipping for today.")
                        skip_zero_qty.add((sym, "BUY"))
                    else:
                        tradingsymbol = watchlist[sym]["tradingsymbol"]
                        tok = watchlist[sym]["token"]
                        kind = "GAP" if is_gap_buy[sym] else "INTRADAY"
                        # Never buy above the trigger threshold -- guards the gap case,
                        # where ltp (from this same cycle's quote) can occasionally sit
                        # above buy_threshold even though day_open already cleared it.
                        entry_price = buy_threshold[sym] if not ltp else min(ltp, buy_threshold[sym])
                        log.info(f"BUY ENTRY TRIGGER {sym} ({kind}) qty={qty} @ ~{trigger_price:.2f} "
                                 f"order_price={entry_price:.2f}")
                        try:
                            order_id, fill_price, filled_qty = place_entry(
                                client, tradingsymbol, tok, qty, "BUY", tag=f"entry1 {kind}", price=entry_price)
                            fill_price = fill_price or trigger_price
                            filled_qty = filled_qty or qty
                            # No averaging on the BUY side (No-Averaging variant) --
                            # stop loss active immediately from entry1.
                            risk_level = fill_price * (1 - BUY_STOP_LOSS_PCT / 100)
                            st.mark_entry1(state, sym, "BUY", is_gap_buy[sym], fill_price,
                                            filled_qty, order_id, risk_level)
                            log.info(f"{sym} BUY: Leg1 filled qty={filled_qty} @ {fill_price:.2f} "
                                     f"order_id={order_id} risk_level={risk_level:.2f}")
                        except TimeoutError as e:
                            log.critical(f"{sym} BUY: entry order placed but fill status could not "
                                         f"be confirmed ({e}). May be LIVE and untracked -- check "
                                         f"getOrderBook manually. Not retriggering today.")
                            skip_zero_qty.add((sym, "BUY"))
                        except Exception as e:
                            log.error(f"BUY entry order FAILED for {sym}: {e} -- skipping trigger.")
                            if "AB4036" in str(e):
                                skip_zero_qty.add((sym, "BUY"))
                                log.warning(f"{sym} BUY: AB4036 (exchange cautionary listing) is a "
                                            f"permanent rejection -- auto-skipping further BUY "
                                            f"attempts on {sym} for today.")

            # ================= SHORT SIDE: AVERAGING + HARD STOP =================
            short_open = st.open_positions(state, "SHORT")
            key_short = f"{sym}|SHORT"
            if key_short in short_open:
                pos = short_open[key_short]
                tradingsymbol = watchlist[sym]["tradingsymbol"]
                tok = watchlist[sym]["token"]

                if pos["is_gap"] and pos["leg2_price"] is None and pos["risk_level"] is None:
                    leg2_trigger = pos["entry1_price"] * (1 + SHORT_AVERAGE_TRIGGER_PCT / 100)
                    if ltp and ltp >= leg2_trigger:
                        qty2 = int(BUYING_POWER // ltp) if ltp > 0 else 0
                        if qty2 >= 1:
                            # Same guard as entry1 -- never sell leg2 below its own
                            # averaging trigger level.
                            leg2_price = max(ltp, leg2_trigger)
                            log.info(f"SHORT AVERAGING TRIGGER {sym} leg2 qty={qty2} @ ~{ltp:.2f} "
                                     f"order_price={leg2_price:.2f}")
                            try:
                                order_id2, fill_price2, filled_qty2 = place_entry(
                                    client, tradingsymbol, tok, qty2, "SHORT", tag="entry2 averaging", price=leg2_price)
                                fill_price2 = fill_price2 or ltp
                                filled_qty2 = filled_qty2 or qty2
                                st.mark_leg2(state, sym, "SHORT", fill_price2, filled_qty2, order_id2,
                                             risk_pct=SHORT_HARD_STOP_PCT)
                                pos = st.open_positions(state, "SHORT")[key_short]
                                log.info(f"{sym} SHORT: Leg2 filled qty={filled_qty2} @ {fill_price2:.2f} "
                                         f"new avg_entry={pos['avg_entry_price']:.2f} "
                                         f"risk_level={pos['risk_level']:.2f}")
                            except TimeoutError as e:
                                log.critical(f"{sym} SHORT: Leg2 order placed but fill status could "
                                             f"not be confirmed ({e}). REAL SHARES MAY BE LIVE untracked. "
                                             f"Check getOrderBook manually right away.")
                                pos["risk_level"] = pos["entry1_price"] * (1 + SHORT_HARD_STOP_PCT / 100)
                            except Exception as e:
                                log.error(f"SHORT Leg2 averaging order FAILED for {sym}: {e} "
                                          f"-- staying single-leg, falling back to entry1-based stop.")
                                pos["risk_level"] = pos["entry1_price"] * (1 + SHORT_HARD_STOP_PCT / 100)
                        else:
                            log.warning(f"{sym} SHORT: averaging trigger fired but qty2 rounds to 0 "
                                        f"-- treating as single-leg, activating stop from entry1.")
                            pos["risk_level"] = pos["entry1_price"] * (1 + SHORT_HARD_STOP_PCT / 100)

                if pos["risk_level"] is not None and ltp and ltp >= pos["risk_level"]:
                    log.info(f"SHORT HARD STOP HIT {sym} ltp={ltp:.2f} >= {pos['risk_level']:.2f}")
                    order_id, fill_price, filled_qty = place_exit_with_retries(
                        client, tradingsymbol, tok, pos["total_qty"], "SHORT", tag="hard stop cover", price=ltp)
                    if order_id is not None:
                        fill_price = fill_price or pos["risk_level"]
                        st.mark_exit(state, sym, "SHORT", fill_price, order_id, "HARD_STOP")
                        log.info(f"{sym} SHORT: covered @ {fill_price:.2f}")

            # ================= BUY SIDE: STOP LOSS (no averaging) =================
            buy_open = st.open_positions(state, "BUY")
            key_buy = f"{sym}|BUY"
            if key_buy in buy_open:
                pos = buy_open[key_buy]
                tradingsymbol = watchlist[sym]["tradingsymbol"]
                tok = watchlist[sym]["token"]

                if pos["risk_level"] is not None and ltp and ltp <= pos["risk_level"]:
                    log.info(f"BUY STOP LOSS HIT {sym} ltp={ltp:.2f} <= {pos['risk_level']:.2f}")
                    order_id, fill_price, filled_qty = place_exit_with_retries(
                        client, tradingsymbol, tok, pos["total_qty"], "BUY", tag="stop loss close", price=ltp)
                    if order_id is not None:
                        fill_price = fill_price or pos["risk_level"]
                        st.mark_exit(state, sym, "BUY", fill_price, order_id, "STOP_LOSS")
                        log.info(f"{sym} BUY: closed @ {fill_price:.2f}")

        # ---------------- EOD: bot does NOT square off (2026-07-29) ----------------
        # The bot no longer places its own EOD exit orders -- Angel One rejects
        # new INTRADAY order requests in a window right before close ("Intraday
        # orders are not allowed near market close... Any open positions will
        # be auto squared off by system"), so every attempt here was landing in
        # that dead zone, failing all retries, while the broker's own MIS
        # auto-square-off closed the position anyway. Per explicit instruction:
        # entries, averaging, and mid-day stop-loss stay bot-managed; the final
        # end-of-day close is left entirely to the broker.
        if now_t >= EOD_SQUAREOFF_TIME:
            open_positions = st.open_positions(state)   # both directions
            if open_positions:
                log.info(f"EOD reached. {len(open_positions)} position(s) still open -- "
                         f"leaving these for the broker's automatic MIS square-off, "
                         f"bot is NOT placing exit orders. Note: these will remain "
                         f"marked 'open' in today's state file even though the broker "
                         f"will close them.")
            log.info("EOD pass complete (no bot-initiated exits). Ending today's run.")
            break

        time.sleep(POLL_INTERVAL_SECONDS)

    # ---------------- Daily summary ----------------
    final_state = st.load_today_state()
    closed = [p for p in final_state["positions"].values() if p["status"] == "closed"]
    short_closed = [p for p in closed if p["direction"] == "SHORT"]
    buy_closed = [p for p in closed if p["direction"] == "BUY"]
    total_pnl = round(sum(p["pnl"] or 0 for p in closed), 2)
    short_pnl = round(sum(p["pnl"] or 0 for p in short_closed), 2)
    buy_pnl = round(sum(p["pnl"] or 0 for p in buy_closed), 2)
    wins = sum(1 for p in closed if (p["pnl"] or 0) > 0)
    log.info(f"DAY SUMMARY: {len(closed)} trade(s) ({len(short_closed)} SHORT, {len(buy_closed)} BUY), "
             f"{wins} winner(s), total P&L = Rs {total_pnl} (SHORT Rs {short_pnl}, BUY Rs {buy_pnl})")


def emergency_squareoff_all(client):
    """Best-effort last resort if the main loop crashes unexpectedly --
    tries once to close every open position (both directions) before the
    process dies."""
    try:
        state = st.load_today_state()
        open_positions = st.open_positions(state)
        if not open_positions:
            return
        log.critical(f"EMERGENCY SQUARE-OFF: attempting to close {len(open_positions)} "
                     f"open position(s) after an unexpected crash.")
        cache = load_token_cache()
        for key, pos in open_positions.items():
            sym = pos["symbol"]
            direction = pos["direction"]
            token = cache.get(sym)
            if not token:
                log.critical(f"  No cached token for {sym} -- cannot auto-close. "
                             f"Relying on broker's MIS auto-square-off.")
                continue
            tradingsymbol = f"{sym}-EQ"
            txn = "BUY" if direction == "SHORT" else "SELL"
            try:
                order_id, fill_price, _ = ords.place_and_confirm(
                    client, _request, tradingsymbol, token, txn, pos["total_qty"], EXCHANGE)
                st.mark_exit(state, sym, direction, fill_price or pos["avg_entry_price"], order_id,
                             "EMERGENCY_SQUAREOFF")
                log.critical(f"  {sym} {direction}: emergency-closed @ {fill_price}")
            except Exception as e:
                log.critical(f"  {sym} {direction}: emergency close FAILED: {e}. "
                             f"Relying on broker's MIS auto-square-off.")
    except Exception as e:
        log.critical(f"emergency_squareoff_all itself failed: {e}. "
                     f"Relying entirely on broker's MIS auto-square-off.")


def main():
    try:
        run_trading_day()
    except SystemExit:
        raise
    except Exception as e:
        log.critical(f"UNHANDLED EXCEPTION in main loop: {e}", exc_info=True)
        try:
            client = login_with_retries()
            emergency_squareoff_all(client)
        except Exception as e2:
            log.critical(f"Emergency square-off itself could not run: {e2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
