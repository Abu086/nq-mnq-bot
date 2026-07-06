"""
Strategy Four — Live Breakout Bot v2 (MIS Intraday)
=====================================================
LIVE TRADING — REAL MONEY

Parameters (confirmed after backtesting):
- Universe    : NIFTY 500 → previous day's top 100 by traded value
- Candle      : 15-minute
- Lookback    : 5 candles
- Filters     : 1) Volume >= 1.5x avg
                2) Breakout magnitude >= 0.3%
                3) Trend alignment (20-MA)
                4) Tight consolidation < 1%
                5) Candle body >= 0.3% (NEW)
                6) ROC > 0.5% in breakout direction (NEW)
- Capital     : Rs.23,000 own per trade (5x MIS = Rs.1,15,000 effective)
- Total budget: Rs.84,000 → max 3 simultaneous positions
- SL          : 0.5%
- Target      : 2.0%
- Monitor     : Every 5 minutes
- Force exit  : 3:15 PM IST
- Max 1 trade per stock per day
- Ranking     : Cached from previous day to avoid 500 API calls at startup
"""

import datetime
import json
import logging
import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd

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

IST = ZoneInfo("Asia/Kolkata")

# ── Strategy Parameters ───────────────────────────────────────────────────
BUDGET           = 23_000      # Own capital per trade
MARGIN_MULTIPLE  = 5           # MIS leverage
DAILY_BUDGET     = 84_000      # Total own capital
MAX_CONCURRENT   = int(DAILY_BUDGET // BUDGET)  # 3 positions
LOOKBACK         = 5
SL_PCT           = 0.005       # 0.5%
TARGET_PCT       = 0.020       # 2.0%
VOL_MULTIPLE     = 1.5
BREAKOUT_MIN_PCT = 0.3
CONSOLIDATION_MAX= 1.0
CANDLE_BODY_MIN  = 0.003       # 0.3%
ROC_MIN          = 0.5         # 0.5%
CANDLE_INTERVAL  = "FIFTEEN_MINUTE"
FORCE_EXIT_TIME  = datetime.time(15, 15)
MARKET_OPEN      = datetime.time(9, 15)
SCAN_INTERVAL    = 300         # 5 minutes
TOKEN_CACHE_FILE = "nifty500_token_cache.json"
RANKING_CACHE    = "top100_ranking_cache.json"
CANDIDATE_CSV    = "ind_nifty500list.csv"
TOP_N            = 100

INDIA_HOLIDAYS_2026 = {
    datetime.date(2026,  1, 26), datetime.date(2026,  2, 26),
    datetime.date(2026,  3, 17), datetime.date(2026,  4,  2),
    datetime.date(2026,  4, 14), datetime.date(2026,  4, 30),
    datetime.date(2026,  8, 15), datetime.date(2026,  8, 27),
    datetime.date(2026, 10,  2), datetime.date(2026, 10, 20),
    datetime.date(2026, 11,  9), datetime.date(2026, 11, 10),
    datetime.date(2026, 11, 25), datetime.date(2026, 12, 25),
}

MAX_RETRIES   = 5
RETRY_BACKOFF = [2, 4, 8, 16, 32]

def is_trading_day(dt: datetime.date) -> bool:
    return dt.weekday() < 5 and dt not in INDIA_HOLIDAYS_2026

# ── API Helpers ───────────────────────────────────────────────────────────
def fetch_candles(client, exchange, token, interval, from_date, to_date):
    body = {"exchange": exchange, "symboltoken": token,
            "interval": interval, "fromdate": from_date, "todate": to_date}
    for attempt in range(MAX_RETRIES):
        resp = _request("POST",
                        "/rest/secure/angelbroking/historical/v1/getCandleData",
                        client._headers(auth=True), body)
        if resp.get("status"):
            return resp.get("data")
        msg = str(resp.get("message", ""))
        if "rate" in msg.lower() and attempt < MAX_RETRIES - 1:
            wait = RETRY_BACKOFF[attempt]
            log.warning(f"Rate limit, retry {attempt+1} in {wait}s")
            time.sleep(wait)
            continue
        log.warning(f"Candle fetch failed: {msg}")
        return None
    return None

def place_order(client, symbol, token, qty, side, price=0):
    import urllib.request
    body = {
        "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": token,
        "transactiontype": side, "exchange": "NSE", "ordertype": "MARKET",
        "producttype": "INTRADAY", "duration": "DAY",
        "price": "0", "squareoff": "0", "stoploss": "0", "quantity": str(qty),
    }
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": os.environ["ANGEL_API_KEY"],
        "Authorization": f"Bearer {client.jwt_token}",
    }
    try:
        url = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder"
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if result.get("status"):
            oid = result["data"]["orderid"]
            log.info(f"✅ {side} {symbol} x{qty} → Order {oid}")
            return oid
        log.error(f"❌ Order failed: {result.get('message')}")
        return None
    except Exception as e:
        log.error(f"❌ Order error: {e}")
        return None

def get_ltp(client, symbol, token):
    try:
        return client.get_ltp("NSE", symbol, token)
    except:
        return None

# ── Token Cache ───────────────────────────────────────────────────────────
def load_tokens():
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE) as f:
            return json.load(f)
    return {}

def resolve_tokens(client, symbols):
    token_map = load_tokens()
    missing = [s for s in symbols if s not in token_map]
    if not missing:
        log.info(f"✅ All {len(symbols)} tokens from cache")
        return {s: token_map[s] for s in symbols if s in token_map}
    log.info(f"Resolving {len(missing)} missing tokens...")
    for sym in missing:
        try:
            result = client.search_scrip("NSE", f"{sym}-EQ")
            if result:
                token_map[sym] = result[0]["symboltoken"]
        except:
            pass
        time.sleep(1.0)
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(token_map, f)
    return {s: token_map[s] for s in symbols if s in token_map}

# ── Ranking (cached) ──────────────────────────────────────────────────────
def load_candidates():
    df = pd.read_csv(CANDIDATE_CSV)
    col = [c for c in df.columns if c.strip().lower() == "symbol"][0]
    return df[col].dropna().astype(str).str.strip().tolist()

def get_universe(client, token_map):
    """
    Load today's stock universe from cache if available.
    If cache is missing or empty, rank NIFTY 500 stocks by previous
    trading day's traded value using Yahoo Finance (reliable, no rate limits).
    """
    # Always use cache if it has stocks — date check removed since we
    # pre-build the cache the evening before via update_universe.py
    if os.path.exists(RANKING_CACHE):
        with open(RANKING_CACHE) as f:
            cache = json.load(f)
        if cache.get("symbols"):
            log.info(f"✅ Universe loaded from cache: {len(cache['symbols'])} stocks")
            return cache["symbols"]

    # Cache miss — rank via Yahoo Finance (no Angel One API calls needed)
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    while yesterday.weekday() >= 5 or yesterday in INDIA_HOLIDAYS_2026:
        yesterday -= datetime.timedelta(days=1)

    date_str = yesterday.strftime("%Y-%m-%d")
    log.info(f"Cache miss — ranking by {date_str} via Yahoo Finance...")

    candidates = pd.read_csv(CANDIDATE_CSV)
    col = [c for c in candidates.columns if c.strip().lower() == "symbol"][0]
    symbols = candidates[col].dropna().astype(str).str.strip().tolist()

    rows = []
    for sym in symbols:
        try:
            import urllib.request as _ur
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=5d"
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _ur.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            result = data["chart"]["result"][0]
            timestamps = result["timestamp"]
            closes  = result["indicators"]["quote"][0]["close"]
            volumes = result["indicators"]["quote"][0]["volume"]
            import datetime as _dt
            for j, ts in enumerate(timestamps):
                day = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                if day == date_str and closes[j] and volumes[j]:
                    rows.append({"symbol": sym, "traded_value": closes[j] * volumes[j]})
                    break
        except:
            pass
        time.sleep(0.3)

    if not rows:
        log.error("Could not rank stocks via Yahoo Finance either. Aborting.")
        return []

    top = pd.DataFrame(rows).nlargest(TOP_N, "traded_value")["symbol"].tolist()
    with open(RANKING_CACHE, "w") as f:
        json.dump({"date": str(datetime.date.today()), "symbols": top}, f)
    log.info(f"✅ Ranked {len(top)} stocks via Yahoo Finance")
    return top

# ── Signal Detection ──────────────────────────────────────────────────────
def check_signal(df, ts):
    """Check all 6 filters on the latest candle. Returns (direction, entry_price) or None."""
    today = df.index[-1].date()
    today_df = df[df.index.date == today].copy()

    if len(today_df) < LOOKBACK + 1:
        return None

    today_df["prior_high"]      = today_df["High"].rolling(LOOKBACK).max().shift(1)
    today_df["prior_low"]       = today_df["Low"].rolling(LOOKBACK).min().shift(1)
    today_df["vol_avg20"]       = today_df["Volume"].rolling(20).mean().shift(1)
    today_df["ma20"]            = today_df["Close"].rolling(20).mean().shift(1)
    today_df["range_width_pct"] = (
        (today_df["High"].rolling(LOOKBACK).max() -
         today_df["Low"].rolling(LOOKBACK).min()) /
        today_df["prior_high"] * 100
    ).shift(1)

    if ts not in today_df.index:
        return None
    row = today_df.loc[ts]

    if pd.isna(row["prior_high"]) or pd.isna(row["vol_avg20"]) or pd.isna(row["ma20"]):
        return None

    # Breakout detection
    if row["High"] > row["prior_high"]:
        direction, entry_price = "LONG", row["prior_high"]
    elif row["Low"] < row["prior_low"]:
        direction, entry_price = "SHORT", row["prior_low"]
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

    # Filter 5: Candle body strength
    if abs(row["Close"] - row["Open"]) < entry_price * CANDLE_BODY_MIN:
        return None

    # Filter 6: ROC momentum
    roc = today_df["Close"].pct_change(5).shift(1).loc[ts] * 100
    if pd.isna(roc):
        return None
    if direction == "LONG" and roc < ROC_MIN:
        return None
    if direction == "SHORT" and roc > -ROC_MIN:
        return None

    return direction, round(entry_price, 2)

# ── Position ──────────────────────────────────────────────────────────────
class Position:
    def __init__(self, symbol, token, direction, entry_price, qty, stop, target, order_id, entry_time):
        self.symbol = symbol; self.token = token
        self.direction = direction; self.entry_price = entry_price
        self.qty = qty; self.stop = stop; self.target = target
        self.order_id = order_id; self.entry_time = entry_time
        self.exit_price = None; self.exit_time = None; self.exit_reason = None

    def check_exit(self, ltp, now):
        if self.direction == "LONG":
            if ltp >= self.target:
                self.exit_price, self.exit_reason, self.exit_time = self.target, "TARGET", now; return True
            if ltp <= self.stop:
                self.exit_price, self.exit_reason, self.exit_time = self.stop, "STOP LOSS", now; return True
        else:
            if ltp <= self.target:
                self.exit_price, self.exit_reason, self.exit_time = self.target, "TARGET", now; return True
            if ltp >= self.stop:
                self.exit_price, self.exit_reason, self.exit_time = self.stop, "STOP LOSS", now; return True
        if now.time() >= FORCE_EXIT_TIME:
            self.exit_price, self.exit_reason, self.exit_time = ltp, "FORCE EXIT (EOD)", now; return True
        return False

    def pnl(self):
        if self.exit_price is None: return 0.0
        if self.direction == "LONG":
            return round((self.exit_price - self.entry_price) * self.qty, 2)
        return round((self.entry_price - self.exit_price) * self.qty, 2)

# ── Journal ───────────────────────────────────────────────────────────────
def log_trade(pos):
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        JOURNAL = "trading_journal.xlsx"
        if not os.path.exists(JOURNAL):
            return
        wb = openpyxl.load_workbook(JOURNAL)
        sheet = "Strategy4_Live_v2"
        if sheet not in wb.sheetnames:
            ws = wb.create_sheet(sheet)
            headers = ["Date","Symbol","Direction","Entry Time","Exit Time",
                      "Entry Price","Stop","Target","Exit Price","Qty",
                      "Gross P&L","Brokerage","Net P&L","Exit Reason"]
            for i, h in enumerate(headers, 1):
                c = ws.cell(row=1, column=i, value=h)
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", start_color="1F4E79")
        else:
            ws = wb[sheet]
        gross = pos.pnl()
        brok  = 40
        net   = gross - brok
        row   = ws.max_row + 1
        data  = [
            pos.entry_time.strftime("%Y-%m-%d"), pos.symbol, pos.direction,
            pos.entry_time.strftime("%H:%M IST"),
            pos.exit_time.strftime("%H:%M IST") if pos.exit_time else "-",
            pos.entry_price, pos.stop, pos.target, pos.exit_price,
            pos.qty, gross, brok, net, pos.exit_reason
        ]
        fill = PatternFill("solid", start_color="C6EFCE" if net > 0 else "FFC7CE")
        for i, v in enumerate(data, 1):
            c = ws.cell(row=row, column=i, value=v)
            c.alignment = Alignment(horizontal="center")
            if i == 13: c.fill = fill
        wb.save(JOURNAL)
        log.info(f"✅ Logged: {pos.symbol} {pos.direction} Net Rs.{net:,.2f}")
    except Exception as e:
        log.warning(f"Journal log failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info("  STRATEGY FOUR LIVE BOT v2 — STARTING")
    log.info(f"  Budget: Rs.{DAILY_BUDGET:,} | Per trade: Rs.{BUDGET:,} | Max concurrent: {MAX_CONCURRENT}")
    log.info(f"  Filters: Volume + Magnitude + Trend + Consolidation + Body + ROC")
    log.info("=" * 65)

    today = datetime.date.today()
    if not is_trading_day(today):
        log.info(f"{today} is not a trading day.")
        return "SKIPPED"

    client = AngelOneClient()
    if not client.login():
        log.error("Login failed.")
        return "ERROR"
    log.info("✅ Angel One login successful")

    candidates = load_candidates()
    token_map  = resolve_tokens(client, candidates)
    if not token_map:
        log.error("No tokens. Aborting.")
        return "ERROR"

    universe = get_universe(client, token_map)
    if not universe:
        log.error("No universe. Aborting.")
        return "ERROR"
    log.info(f"Trading {len(universe)} stocks today")

    open_positions  = {}
    closed_positions = []
    traded_today    = set()
    daily_pnl       = 0.0

    log.info(f"\nScanning every {SCAN_INTERVAL//60} minutes | Force exit at {FORCE_EXIT_TIME}")

    while True:
        now   = datetime.datetime.now(IST)
        now_t = now.time()

        if now_t >= FORCE_EXIT_TIME:
            for sym, pos in list(open_positions.items()):
                ltp = get_ltp(client, sym, token_map.get(sym, ""))
                if ltp:
                    side = "SELL" if pos.direction == "LONG" else "BUY"
                    place_order(client, sym, token_map[sym], pos.qty, side)
                    pos.exit_price = ltp; pos.exit_time = now
                    pos.exit_reason = "FORCE EXIT (EOD)"
                    daily_pnl += pos.pnl()
                    closed_positions.append(pos)
                    log_trade(pos)
                time.sleep(0.5)
            break

        if now_t < MARKET_OPEN:
            time.sleep(30)
            continue

        # Monitor open positions
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            ltp = get_ltp(client, sym, token_map.get(sym, ""))
            if ltp and pos.check_exit(ltp, now):
                side = "SELL" if pos.direction == "LONG" else "BUY"
                place_order(client, sym, token_map[sym], pos.qty, side)
                daily_pnl += pos.pnl()
                log.info(f"  {'🎯' if 'TARGET' in pos.exit_reason else '🛑'} "
                         f"{sym} {pos.exit_reason} | P&L: Rs.{pos.pnl():,.2f}")
                closed_positions.append(pos)
                log_trade(pos)
                del open_positions[sym]
            time.sleep(0.3)

        # Scan for new signals
        if len(open_positions) < MAX_CONCURRENT:
            log.info(f"  [{now.strftime('%H:%M')}] Scanning {len(universe)} stocks "
                     f"| Open: {len(open_positions)}/{MAX_CONCURRENT} "
                     f"| P&L: Rs.{daily_pnl:,.2f}")

            for sym in universe:
                if sym in traded_today or sym in open_positions:
                    continue
                if len(open_positions) >= MAX_CONCURRENT:
                    break

                token = token_map.get(sym)
                if not token:
                    continue

                from_str = f"{today.strftime('%Y-%m-%d')} 09:00"
                to_str   = now.strftime("%Y-%m-%d %H:%M")
                candles  = fetch_candles(client, "NSE", token, CANDLE_INTERVAL, from_str, to_str)
                if not candles or len(candles) < LOOKBACK + 2:
                    time.sleep(0.3)
                    continue

                df = pd.DataFrame(candles, columns=["timestamp","Open","High","Low","Close","Volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.set_index("timestamp")
                ts = df.index[-1]

                signal = check_signal(df, ts)
                if not signal:
                    time.sleep(0.2)
                    continue

                direction, entry_price = signal
                ltp = get_ltp(client, sym, token)
                if not ltp:
                    continue

                effective = BUDGET * MARGIN_MULTIPLE
                qty = max(1, int(effective // ltp))

                if direction == "LONG":
                    stop = round(entry_price * (1 - SL_PCT), 2)
                    target = round(entry_price * (1 + TARGET_PCT), 2)
                    side = "BUY"
                else:
                    stop = round(entry_price * (1 + SL_PCT), 2)
                    target = round(entry_price * (1 - TARGET_PCT), 2)
                    side = "SELL"

                log.info(f"  🚀 SIGNAL: {sym} {direction} @ {entry_price} | SL:{stop} | T:{target} | Qty:{qty}")
                order_id = place_order(client, sym, token, qty, side, ltp)
                if order_id:
                    open_positions[sym] = Position(sym, token, direction, entry_price,
                                                   qty, stop, target, order_id, now)
                    traded_today.add(sym)
                time.sleep(0.5)

        time.sleep(SCAN_INTERVAL)

    # EOD Summary
    log.info("\n" + "=" * 65)
    log.info(f"  END OF DAY | Trades: {len(closed_positions)} | Gross P&L: Rs.{daily_pnl:,.2f}")
    brok = 40 * len(closed_positions)
    log.info(f"  Brokerage: Rs.{brok:,} | Net P&L: Rs.{daily_pnl-brok:,.2f}")
    log.info("=" * 65)
    return "TRADED" if closed_positions else "SKIPPED"

if __name__ == "__main__":
    status = run()
    sys.exit(1 if status == "ERROR" else 0)
