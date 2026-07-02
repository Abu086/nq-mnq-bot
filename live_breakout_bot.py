"""
Strategy Four — Live Breakout Bot (MIS Intraday)
==================================================
- Trades REAL money via Angel One SmartAPI
- Universe: NIFTY 500 → previous day's top 100 by traded value
- Instrument: NSE equity stocks (MIS — Margin Intraday Square-off)
- Candle interval: 15-minute
- Lookback: 5 candles (signals from 10:30 AM onwards)
- Entry: Break above/below prior 5-candle high/low
- Filters (ALL must pass):
    1. Volume ≥ 1.5x 20-period average
    2. Breakout magnitude ≥ 0.3%
    3. Trend alignment — above/below 20-MA
    4. Tight consolidation — prior range < 1%
- Capital: Rs.5,000 own per trade (5x MIS margin = Rs.25,000 effective)
- Total budget: Rs.84,000 → max 16 simultaneous positions
- Rolling capital: when a position closes, slot freed for new trade
- Stop Loss: 0.5% from entry
- Target: 2.0% from entry
- Force exit: 3:15 PM IST (MIS auto square-off by Angel One at 3:20 PM)
- Max 1 trade per stock per day
- Logs all trades to trading_journal.xlsx
"""

import datetime
import json
import logging
import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_ta as ta

from angel_one_client import AngelOneClient, _request

# ── Logging ───────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s IST] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/live_breakout.log"),
    ],
)
log = logging.getLogger(__name__)

IST          = ZoneInfo("Asia/Kolkata")
JOURNAL_FILE = "trading_journal.xlsx"

# ── Config ────────────────────────────────────────────────────────────────
BUDGET           = 5_000       # Rs.5,000 own capital per trade
MARGIN_MULTIPLE  = 5           # Angel One MIS ~5x leverage
DAILY_BUDGET     = 84_000      # Total own capital available
MAX_CONCURRENT   = int(DAILY_BUDGET // BUDGET)   # 16 simultaneous positions
LOOKBACK         = 5           # Prior candles for breakout range
SL_PCT           = 0.005       # 0.5% stop loss
TARGET_PCT       = 0.020       # 2.0% target
VOL_MULTIPLE     = 1.5         # Volume must be 1.5x average
BREAKOUT_MIN_PCT = 0.3         # Breakout magnitude minimum
CONSOLIDATION_MAX= 1.0         # Prior range max width %
CANDLE_INTERVAL  = "FIFTEEN_MINUTE"
FORCE_EXIT_TIME  = datetime.time(15, 15)
MARKET_OPEN      = datetime.time(9, 15)
SCAN_INTERVAL    = 300         # Scan every 5 minutes (300 seconds)
TOKEN_CACHE_FILE = "nifty500_token_cache.json"
CANDIDATE_CSV    = "ind_nifty500list.csv"
TOP_N            = 100

# ── Holiday List 2026 ─────────────────────────────────────────────────────
INDIA_HOLIDAYS_2026 = {
    datetime.date(2026,  1, 26), datetime.date(2026,  2, 26),
    datetime.date(2026,  3, 17), datetime.date(2026,  4,  2),
    datetime.date(2026,  4, 14), datetime.date(2026,  4, 30),
    datetime.date(2026,  8, 15), datetime.date(2026,  8, 27),
    datetime.date(2026, 10,  2), datetime.date(2026, 10, 20),
    datetime.date(2026, 11,  9), datetime.date(2026, 11, 10),
    datetime.date(2026, 11, 25), datetime.date(2026, 12, 25),
}

def is_trading_day(dt: datetime.date) -> bool:
    return dt.weekday() < 5 and dt not in INDIA_HOLIDAYS_2026

# ── API Helpers ───────────────────────────────────────────────────────────
MAX_RETRIES   = 5
RETRY_BACKOFF = [2, 4, 8, 16, 32]

def api_call_with_retry(func, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            result = func(*args, **kwargs)
            if result is not None:
                return result
        except Exception as e:
            if "rate" in str(e).lower() and attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                log.warning(f"Rate limit, retry {attempt+1}/{MAX_RETRIES} in {wait}s")
                time.sleep(wait)
                continue
            log.warning(f"API error: {e}")
            return None
    return None

def fetch_candles(client: AngelOneClient, exchange: str, token: str,
                  interval: str, from_date: str, to_date: str):
    body = {
        "exchange":    exchange,
        "symboltoken": token,
        "interval":    interval,
        "fromdate":    from_date,
        "todate":      to_date,
    }
    for attempt in range(MAX_RETRIES):
        resp = _request("POST",
                        "/rest/secure/angelbroking/historical/v1/getCandleData",
                        client._headers(auth=True), body)
        if resp.get("status"):
            return resp.get("data")
        msg = str(resp.get("message", ""))
        if "rate" in msg.lower() and attempt < MAX_RETRIES - 1:
            wait = RETRY_BACKOFF[attempt]
            log.warning(f"Rate limit fetching candles, retry {attempt+1} in {wait}s")
            time.sleep(wait)
            continue
        log.warning(f"Candle fetch failed: {msg}")
        return None
    return None

def place_order(client: AngelOneClient, symbol: str, token: str,
                qty: int, order_type: str, price: float) -> str | None:
    """Place a real MIS intraday order."""
    body = {
        "variety":         "NORMAL",
        "tradingsymbol":   symbol,
        "symboltoken":     token,
        "transactiontype": order_type,
        "exchange":        "NSE",
        "ordertype":       "MARKET",
        "producttype":     "INTRADAY",
        "duration":        "DAY",
        "price":           "0",
        "squareoff":       "0",
        "stoploss":        "0",
        "quantity":        str(qty),
    }
    import urllib.request
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder"
    headers = {
        "Content-Type":     "application/json",
        "Accept":           "application/json",
        "X-UserType":       "USER",
        "X-SourceID":       "WEB",
        "X-ClientLocalIP":  "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress":     "00:00:00:00:00:00",
        "X-PrivateKey":     os.environ["ANGEL_API_KEY"],
        "Authorization":    f"Bearer {client.jwt_token}",
    }
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if result.get("status"):
            order_id = result["data"]["orderid"]
            log.info(f"✅ {order_type} {symbol} x{qty} → Order ID: {order_id}")
            return order_id
        else:
            log.error(f"❌ Order failed for {symbol}: {result.get('message')}")
            return None
    except Exception as e:
        log.error(f"❌ Order error for {symbol}: {e}")
        return None

def get_ltp(client: AngelOneClient, symbol: str, token: str) -> float | None:
    """Get last traded price for a stock."""
    try:
        return client.get_ltp("NSE", symbol, token)
    except Exception as e:
        log.warning(f"LTP fetch failed for {symbol}: {e}")
        return None

# ── Token Cache ───────────────────────────────────────────────────────────
def load_token_cache() -> dict:
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_token_cache(token_map: dict):
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(token_map, f)

def resolve_tokens(client: AngelOneClient, symbols: list) -> dict:
    """Resolve symbols to tokens, using cache where possible."""
    token_map = load_token_cache()
    missing = [s for s in symbols if s not in token_map]
    if not missing:
        log.info(f"✅ All {len(symbols)} tokens loaded from cache")
        return {s: token_map[s] for s in symbols if s in token_map}
    log.info(f"Resolving {len(missing)} new symbols...")
    for i, sym in enumerate(missing):
        result = api_call_with_retry(client.search_scrip, "NSE", f"{sym}-EQ")
        if result:
            token_map[sym] = result[0]["symboltoken"]
        time.sleep(1.0)
    save_token_cache(token_map)
    return {s: token_map[s] for s in symbols if s in token_map}

# ── Stock Universe ────────────────────────────────────────────────────────
def load_candidates() -> list:
    if not os.path.exists(CANDIDATE_CSV):
        raise RuntimeError(f"'{CANDIDATE_CSV}' not found. Download from niftyindices.com via browser.")
    df = pd.read_csv(CANDIDATE_CSV)
    col = [c for c in df.columns if c.strip().lower() == "symbol"]
    return df[col[0]].dropna().astype(str).str.strip().tolist()

def get_previous_day_top100(client: AngelOneClient, token_map: dict) -> list:
    """
    Rank NIFTY 500 stocks by YESTERDAY's traded value.
    Returns top 100 symbols to trade today.
    """
    today = datetime.date.today()
    # Get yesterday (skip weekends)
    yesterday = today - datetime.timedelta(days=1)
    while yesterday.weekday() >= 5 or yesterday in INDIA_HOLIDAYS_2026:
        yesterday -= datetime.timedelta(days=1)

    from_str = f"{yesterday.strftime('%Y-%m-%d')} 09:00"
    to_str   = f"{yesterday.strftime('%Y-%m-%d')} 15:30"

    log.info(f"Ranking stocks by {yesterday} traded value...")
    rows = []
    for sym, token in token_map.items():
        candles = fetch_candles(client, "NSE", token, "ONE_DAY", from_str, to_str)
        if candles and len(candles) > 0:
            c = candles[-1]
            close  = float(c[4])
            volume = float(c[5])
            rows.append({"symbol": sym, "token": token,
                         "traded_value": close * volume})
        time.sleep(0.3)

    if not rows:
        log.error("Could not fetch any ranking data!")
        return []

    df = pd.DataFrame(rows).nlargest(TOP_N, "traded_value")
    top100 = df["symbol"].tolist()
    log.info(f"Today's top {TOP_N} stocks selected: {top100[:5]}... (and {len(top100)-5} more)")
    return top100

# ── Signal Detection ──────────────────────────────────────────────────────
def check_signal(df: pd.DataFrame) -> tuple | None:
    """
    Check if the latest candle produces a valid breakout signal
    with all 4 filters. Returns (direction, entry_price) or None.
    """
    if len(df) < max(LOOKBACK + 1, 21):
        return None

    # Only use today's candles for the lookback
    today = df.index[-1].date()
    today_df = df[df.index.date == today].copy()

    if len(today_df) < LOOKBACK + 1:
        return None  # Not enough today's candles yet

    # Rolling calculations on today's data only
    today_df["prior_high"]      = today_df["High"].rolling(LOOKBACK).max().shift(1)
    today_df["prior_low"]       = today_df["Low"].rolling(LOOKBACK).min().shift(1)
    today_df["vol_avg20"]       = today_df["Volume"].rolling(20).mean().shift(1)
    today_df["ma20"]            = today_df["Close"].rolling(20).mean().shift(1)
    today_df["range_width_pct"] = (
        (today_df["High"].rolling(LOOKBACK).max() -
         today_df["Low"].rolling(LOOKBACK).min()) /
        today_df["prior_high"] * 100
    ).shift(1)

    row = today_df.iloc[-1]

    if pd.isna(row["prior_high"]) or pd.isna(row["vol_avg20"]) or pd.isna(row["ma20"]):
        return None

    # Breakout detection
    direction = None
    if row["High"] > row["prior_high"]:
        direction   = "LONG"
        entry_price = row["prior_high"]
    elif row["Low"] < row["prior_low"]:
        direction   = "SHORT"
        entry_price = row["prior_low"]
    else:
        return None

    # Filter 1: Volume
    if row["Volume"] < VOL_MULTIPLE * row["vol_avg20"]:
        return None

    # Filter 2: Breakout magnitude
    if direction == "LONG":
        mag = (row["High"] - row["prior_high"]) / row["prior_high"] * 100
    else:
        mag = (row["prior_low"] - row["Low"]) / row["prior_low"] * 100
    if mag < BREAKOUT_MIN_PCT:
        return None

    # Filter 3: Trend alignment
    if direction == "LONG" and row["Close"] < row["ma20"]:
        return None
    if direction == "SHORT" and row["Close"] > row["ma20"]:
        return None

    # Filter 4: Tight consolidation
    if pd.isna(row["range_width_pct"]) or row["range_width_pct"] > CONSOLIDATION_MAX:
        return None

    return direction, round(entry_price, 2)

# ── Position Manager ──────────────────────────────────────────────────────
class Position:
    def __init__(self, symbol, token, direction, entry_price, qty,
                 stop, target, order_id, entry_time):
        self.symbol      = symbol
        self.token       = token
        self.direction   = direction
        self.entry_price = entry_price
        self.qty         = qty
        self.stop        = stop
        self.target      = target
        self.order_id    = order_id
        self.entry_time  = entry_time
        self.exit_price  = None
        self.exit_time   = None
        self.exit_reason = None

    def check_exit(self, ltp: float, now: datetime.datetime) -> bool:
        """Returns True if position should be closed."""
        if self.direction == "LONG":
            if ltp >= self.target:
                self.exit_price  = self.target
                self.exit_reason = "TARGET"
                self.exit_time   = now
                return True
            if ltp <= self.stop:
                self.exit_price  = self.stop
                self.exit_reason = "STOP LOSS"
                self.exit_time   = now
                return True
        else:
            if ltp <= self.target:
                self.exit_price  = self.target
                self.exit_reason = "TARGET"
                self.exit_time   = now
                return True
            if ltp >= self.stop:
                self.exit_price  = self.stop
                self.exit_reason = "STOP LOSS"
                self.exit_time   = now
                return True

        if now.time() >= FORCE_EXIT_TIME:
            self.exit_price  = ltp
            self.exit_reason = "FORCE EXIT (EOD)"
            self.exit_time   = now
            return True

        return False

    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        if self.direction == "LONG":
            return round((self.exit_price - self.entry_price) * self.qty, 2)
        else:
            return round((self.entry_price - self.exit_price) * self.qty, 2)

# ── Journal Logging ───────────────────────────────────────────────────────
def log_trade_to_journal(pos: Position):
    """Append completed trade to trading_journal.xlsx."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side

    sheet_name = "Strategy4_Breakout_Live"
    cols = [
        "Date", "Symbol", "Direction", "Entry Time", "Exit Time",
        "Entry Price", "Stop Loss", "Target", "Exit Price",
        "Qty", "Gross P&L (Rs.)", "Brokerage (Rs.)", "Net P&L (Rs.)",
        "Exit Reason"
    ]

    gross_pnl  = pos.pnl()
    brokerage  = 20 * 2   # Rs.20 flat × 2 orders
    net_pnl    = round(gross_pnl - brokerage, 2)

    row_data = [
        pos.entry_time.strftime("%Y-%m-%d"),
        pos.symbol,
        pos.direction,
        pos.entry_time.strftime("%H:%M IST"),
        pos.exit_time.strftime("%H:%M IST") if pos.exit_time else "-",
        pos.entry_price,
        pos.stop,
        pos.target,
        pos.exit_price,
        pos.qty,
        gross_pnl,
        brokerage,
        net_pnl,
        pos.exit_reason,
    ]

    if not os.path.exists(JOURNAL_FILE):
        log.warning(f"Journal not found: {JOURNAL_FILE} — trade not logged")
        return

    wb = openpyxl.load_workbook(JOURNAL_FILE)
    if sheet_name not in wb.sheetnames:
        ws = wb.create_sheet(sheet_name)
        # Header row
        for i, col in enumerate(cols, 1):
            c = ws.cell(row=1, column=i, value=col)
            c.font = Font(bold=True, color="FFFFFF", name="Arial", size=10)
            c.fill = PatternFill("solid", start_color="1F4E79")
            c.alignment = Alignment(horizontal="center")
    else:
        ws = wb[sheet_name]

    next_row = ws.max_row + 1
    pnl_fill = PatternFill("solid",
                           start_color="C6EFCE" if net_pnl > 0 else "FFC7CE")
    for i, val in enumerate(row_data, 1):
        c = ws.cell(row=next_row, column=i, value=val)
        c.alignment = Alignment(horizontal="center")
        if i == len(cols) - 1:  # Net P&L column
            c.fill = pnl_fill

    wb.save(JOURNAL_FILE)
    log.info(f"✅ Trade logged: {pos.symbol} {pos.direction} "
             f"Net P&L: Rs.{net_pnl:,.2f}")

# ── Main Trading Loop ─────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info("  STRATEGY FOUR — LIVE BREAKOUT BOT STARTING")
    log.info(f"  Budget: Rs.{DAILY_BUDGET:,} | Per trade: Rs.{BUDGET:,} | Max concurrent: {MAX_CONCURRENT}")
    log.info("=" * 65)

    today = datetime.date.today()
    if not is_trading_day(today):
        log.info(f"{today} is not a trading day. Exiting.")
        return "SKIPPED"

    # Login
    client = AngelOneClient()
    if not client.login():
        log.error("Angel One login failed.")
        return "ERROR"
    log.info("✅ Angel One login successful")

    # Load candidates and resolve tokens
    candidates = load_candidates()
    token_map  = resolve_tokens(client, candidates)
    if not token_map:
        log.error("No tokens resolved. Aborting.")
        return "ERROR"

    # Get today's stock universe (previous day's top 100)
    universe = get_previous_day_top100(client, token_map)
    if not universe:
        log.error("Could not determine today's stock universe.")
        return "ERROR"
    log.info(f"Trading universe for today: {len(universe)} stocks")

    # State tracking
    open_positions  = {}   # symbol → Position
    closed_positions = []
    traded_today    = set()  # symbols already traded today
    daily_pnl       = 0.0

    # Pre-fetch today's 15-min candle history (up to now) for each stock
    # We'll update this incrementally during the day
    candle_cache = {}

    log.info("\n  Starting market scan loop...")
    log.info(f"  Monitoring every {SCAN_INTERVAL//60} minutes")
    log.info(f"  Force exit at {FORCE_EXIT_TIME.strftime('%H:%M')} IST\n")

    while True:
        now     = datetime.datetime.now(IST)
        now_t   = now.time()

        # Stop scanning after force exit time
        if now_t >= FORCE_EXIT_TIME:
            # Close any remaining open positions
            if open_positions:
                log.info(f"  ⏰ Force exit time — closing {len(open_positions)} open positions")
                for sym, pos in list(open_positions.items()):
                    ltp = get_ltp(client, sym, token_map[sym])
                    if ltp:
                        place_order(client, sym, token_map[sym],
                                   pos.qty,
                                   "SELL" if pos.direction == "LONG" else "BUY",
                                   ltp)
                        pos.exit_price  = ltp
                        pos.exit_time   = now
                        pos.exit_reason = "FORCE EXIT (EOD)"
                        daily_pnl += pos.pnl()
                        closed_positions.append(pos)
                        log_trade_to_journal(pos)
                    time.sleep(0.5)
            break

        # Wait for market open
        if now_t < MARKET_OPEN:
            time.sleep(30)
            continue

        # ── STEP 1: Monitor existing positions ────────────────────────────
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            ltp = get_ltp(client, sym, token_map.get(sym, ""))
            if ltp is None:
                continue

            if pos.check_exit(ltp, now):
                # Close the position
                side = "SELL" if pos.direction == "LONG" else "BUY"
                place_order(client, sym, token_map[sym], pos.qty, side, ltp)
                daily_pnl += pos.pnl()
                log.info(f"  {'🎯' if pos.exit_reason == 'TARGET' else '🛑'} "
                         f"{sym} {pos.direction} closed: "
                         f"{pos.exit_reason} | P&L: Rs.{pos.pnl():,.2f}")
                closed_positions.append(pos)
                log_trade_to_journal(pos)
                del open_positions[sym]
            time.sleep(0.3)

        # ── STEP 2: Scan for new signals ──────────────────────────────────
        if len(open_positions) < MAX_CONCURRENT:
            available_slots = MAX_CONCURRENT - len(open_positions)
            log.info(f"  [{now.strftime('%H:%M')}] Scanning {len(universe)} stocks "
                     f"| Open: {len(open_positions)}/{MAX_CONCURRENT} "
                     f"| Slots: {available_slots}")

            for sym in universe:
                if sym in traded_today:
                    continue
                if sym in open_positions:
                    continue
                if len(open_positions) >= MAX_CONCURRENT:
                    break

                token = token_map.get(sym)
                if not token:
                    continue

                # Fetch fresh 15-min candles for today
                from_str = f"{today.strftime('%Y-%m-%d')} 09:00"
                to_str   = now.strftime("%Y-%m-%d %H:%M")
                candles  = fetch_candles(client, "NSE", token,
                                         CANDLE_INTERVAL, from_str, to_str)
                if not candles or len(candles) < LOOKBACK + 2:
                    time.sleep(0.2)
                    continue

                df = pd.DataFrame(candles,
                                  columns=["timestamp","Open","High","Low","Close","Volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.set_index("timestamp")

                signal = check_signal(df)
                if signal is None:
                    time.sleep(0.2)
                    continue

                direction, entry_price = signal
                ltp = get_ltp(client, sym, token)
                if ltp is None:
                    continue

                # Calculate position size using real margin
                effective_budget = BUDGET * MARGIN_MULTIPLE  # Rs.25,000
                qty = max(1, int(effective_budget // ltp))

                # Calculate SL and target
                if direction == "LONG":
                    stop   = round(entry_price * (1 - SL_PCT), 2)
                    target = round(entry_price * (1 + TARGET_PCT), 2)
                    side   = "BUY"
                else:
                    stop   = round(entry_price * (1 + SL_PCT), 2)
                    target = round(entry_price * (1 - TARGET_PCT), 2)
                    side   = "SELL"

                log.info(f"  🚀 SIGNAL: {sym} {direction} @ {entry_price} "
                         f"| SL: {stop} | Target: {target} | Qty: {qty}")

                # Place order
                order_id = place_order(client, sym, token, qty, side, ltp)
                if order_id:
                    pos = Position(sym, token, direction, entry_price,
                                  qty, stop, target, order_id, now)
                    open_positions[sym] = pos
                    traded_today.add(sym)
                    log.info(f"  ✅ Position opened: {sym} {direction} "
                             f"x{qty} @ {entry_price}")
                time.sleep(0.5)

        # Sleep until next scan
        log.info(f"  Daily P&L so far: Rs.{daily_pnl:,.2f} | "
                 f"Closed trades: {len(closed_positions)}")
        time.sleep(SCAN_INTERVAL)

    # ── End of day summary ────────────────────────────────────────────────
    log.info("\n" + "=" * 65)
    log.info("  END OF DAY SUMMARY")
    log.info("=" * 65)
    log.info(f"  Total trades    : {len(closed_positions)}")
    wins   = [p for p in closed_positions if p.pnl() > 0]
    losses = [p for p in closed_positions if p.pnl() <= 0]
    log.info(f"  Wins            : {len(wins)}")
    log.info(f"  Losses          : {len(losses)}")
    log.info(f"  Gross P&L       : Rs.{daily_pnl:,.2f}")
    brokerage = 40 * len(closed_positions)
    log.info(f"  Brokerage       : Rs.{brokerage:,}")
    log.info(f"  Net P&L         : Rs.{daily_pnl - brokerage:,.2f}")
    log.info("=" * 65)

    return "TRADED" if closed_positions else "SKIPPED"


if __name__ == "__main__":
    status = run()
    sys.exit(1 if status == "ERROR" else 0)
