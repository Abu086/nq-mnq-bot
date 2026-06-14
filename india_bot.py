"""
NIFTY / BANKNIFTY Paper Trading Bot
====================================
Strategies  : Bull Call Spread (Bullish) OR Bear Put Spread (Bearish)
              — chosen automatically based on trend detection
Trend Logic : EMA 9 vs EMA 21 + Price vs Previous Close
              Both Bullish  → Bull Call Spread
              Both Bearish  → Bear Put Spread
              Conflicting   → SKIP (no trade today)
Entry Time  : 9:20 AM IST (after opening volatility settles)
Expiry      : NIFTY → Weekly (Thursday) | BANKNIFTY → Monthly (last Thursday)
Budget      : ₹50,000 per instrument per day (full budget on ONE strategy)
Data Source : Yahoo Finance (free, no API key)
Journal     : trading_journal.xlsx (shared file, separate sheets)
Mode        : PAPER TRADING — NO REAL ORDERS
"""

import datetime
import json
import logging
import math
import os
import random
import sys
from zoneinfo import ZoneInfo

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s IST] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/india_bot.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Timezone ──────────────────────────────────────────────────────────────────
IST          = ZoneInfo("Asia/Kolkata")
JOURNAL_FILE = "trading_journal.xlsx"

# ── Instrument Config ─────────────────────────────────────────────────────────
INSTRUMENTS = {
    "NIFTY": {
        "ticker":       "^NSEI",
        "lot_size":     75,        # 1 lot = 75 units
        "strike_width": 100,       # 100 point wide spread
        "budget":       50000,     # ₹50,000 — full budget on ONE strategy
        "expiry_type":  "weekly",  # every Thursday
        "stt":          0.0125,    # Securities Transaction Tax % (sell side)
        "sebi_fee":     0.0001,    # SEBI turnover fee %
        "stamp_duty":   0.003,     # Stamp duty % (buy side)
        "brokerage":    20,        # ₹20 flat per order
        "gst_pct":      0.18,      # 18% GST on brokerage + SEBI fee
    },
    "BANKNIFTY": {
        "ticker":       "^NSEBANK",
        "lot_size":     30,        # 1 lot = 30 units
        "strike_width": 200,       # 200 point wide spread
        "budget":       50000,
        "expiry_type":  "monthly", # last Thursday of month
        "stt":          0.0125,
        "sebi_fee":     0.0001,
        "stamp_duty":   0.003,
        "brokerage":    20,
        "gst_pct":      0.18,
    },
}

TARGET_PCT = 0.30   # Exit at 30% of max profit
SL_PCT     = 0.30   # Stop loss at 30% of premium paid

# ── Indian Market Holidays 2026 ───────────────────────────────────────────────
INDIA_HOLIDAYS_2026 = {
    datetime.date(2026,  1, 26),  # Republic Day
    datetime.date(2026,  2, 26),  # Maha Shivaratri
    datetime.date(2026,  3, 17),  # Holi
    datetime.date(2026,  4,  2),  # Ram Navami
    datetime.date(2026,  4, 14),  # Good Friday
    datetime.date(2026,  4, 30),  # Maharashtra Day
    datetime.date(2026,  8, 15),  # Independence Day
    datetime.date(2026,  8, 27),  # Ganesh Chaturthi
    datetime.date(2026, 10,  2),  # Gandhi Jayanti
    datetime.date(2026, 10, 20),  # Dussehra
    datetime.date(2026, 11,  9),  # Diwali Laxmi Puja
    datetime.date(2026, 11, 10),  # Diwali Balipratipada
    datetime.date(2026, 11, 25),  # Guru Nanak Jayanti
    datetime.date(2026, 12, 25),  # Christmas
}


def is_india_market_day(dt: datetime.date) -> bool:
    return dt.weekday() < 5 and dt not in INDIA_HOLIDAYS_2026


# ── Expiry Calculators ────────────────────────────────────────────────────────
def next_thursday(from_date: datetime.date) -> datetime.date:
    days_ahead = (3 - from_date.weekday()) % 7
    return from_date + datetime.timedelta(days=days_ahead if days_ahead else 7)


def get_weekly_expiry(today: datetime.date) -> datetime.date:
    if today.weekday() == 3 and today not in INDIA_HOLIDAYS_2026:
        return today
    return next_thursday(today + datetime.timedelta(days=1)
                         if today.weekday() == 3 else today)


def get_monthly_expiry(today: datetime.date) -> datetime.date:
    def last_thursday(year: int, month: int) -> datetime.date:
        if month == 12:
            last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
        while last_day.weekday() != 3:
            last_day -= datetime.timedelta(days=1)
        return last_day

    expiry = last_thursday(today.year, today.month)
    if today > expiry:
        expiry = last_thursday(
            today.year + 1 if today.month == 12 else today.year,
            1 if today.month == 12 else today.month + 1
        )
    return expiry


def get_expiry(inst_name: str, today: datetime.date) -> datetime.date:
    if INSTRUMENTS[inst_name]["expiry_type"] == "weekly":
        return get_weekly_expiry(today)
    return get_monthly_expiry(today)


# ── EMA Calculator ────────────────────────────────────────────────────────────
def calculate_ema(prices: list, period: int) -> float:
    """Calculate Exponential Moving Average for given period."""
    if len(prices) < period:
        return sum(prices) / len(prices)
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period   # seed with SMA
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)


# ── Price & History Fetcher ───────────────────────────────────────────────────
def fetch_price_history(ticker: str, days: int = 30) -> dict | None:
    """
    Fetch recent closing prices for EMA calculation.
    Returns dict with 'current', 'prev_close', 'closes' list.
    """
    import urllib.request
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&range={days}d"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        result  = data["chart"]["result"][0]
        closes  = result["indicators"]["quote"][0]["close"]
        closes  = [c for c in closes if c is not None]
        if len(closes) < 2:
            return None
        return {
            "current":    round(closes[-1], 2),
            "prev_close": round(closes[-2], 2),
            "closes":     closes,
        }
    except Exception as exc:
        log.warning(f"Price fetch failed for {ticker}: {exc}")
        return None


# ── Trend Detection ───────────────────────────────────────────────────────────
def detect_trend(price_data: dict) -> dict:
    """
    Detect trend using TWO indicators:
    1. Price vs Previous Close
    2. EMA 9 vs EMA 21

    Returns:
        trend   : 'BULLISH', 'BEARISH', or 'SKIP'
        strategy: 'BULL_CALL', 'BEAR_PUT', or None
        reason  : explanation string
    """
    closes    = price_data["closes"]
    current   = price_data["current"]
    prev      = price_data["prev_close"]

    ema9      = calculate_ema(closes, 9)
    ema21     = calculate_ema(closes, 21)

    # Signal 1: Price vs Previous Close
    price_signal = "BULLISH" if current > prev else "BEARISH"

    # Signal 2: EMA 9 vs EMA 21
    ema_signal   = "BULLISH" if ema9 > ema21 else "BEARISH"

    log.info(f"  Price Signal : {price_signal}  (Current: {current} vs Prev: {prev})")
    log.info(f"  EMA Signal   : {ema_signal}  (EMA9: {ema9} vs EMA21: {ema21})")

    if price_signal == "BULLISH" and ema_signal == "BULLISH":
        return {
            "trend":    "BULLISH",
            "strategy": "BULL_CALL",
            "reason":   f"Price↑ ({current}>{prev}) & EMA9↑ ({ema9}>{ema21})",
            "ema9":     ema9,
            "ema21":    ema21,
        }
    elif price_signal == "BEARISH" and ema_signal == "BEARISH":
        return {
            "trend":    "BEARISH",
            "strategy": "BEAR_PUT",
            "reason":   f"Price↓ ({current}<{prev}) & EMA9↓ ({ema9}<{ema21})",
            "ema9":     ema9,
            "ema21":    ema21,
        }
    else:
        return {
            "trend":    "SKIP",
            "strategy": None,
            "reason":   f"Conflicting signals — Price:{price_signal} vs EMA:{ema_signal}",
            "ema9":     ema9,
            "ema21":    ema21,
        }


# ── Fee Calculator ────────────────────────────────────────────────────────────
def calc_india_fees(inst: dict, lots: int, premium: float) -> dict:
    units      = lots * inst["lot_size"]
    turnover   = premium * units
    brokerage  = inst["brokerage"] * 2
    sebi       = inst["sebi_fee"] / 100 * turnover * 2
    stt        = inst["stt"] / 100 * turnover
    stamp      = inst["stamp_duty"] / 100 * turnover
    gst        = inst["gst_pct"] * (brokerage + sebi)
    total      = round(brokerage + sebi + stt + stamp + gst, 2)
    return {
        "brokerage":  round(brokerage, 2),
        "stt":        round(stt, 2),
        "sebi_fee":   round(sebi, 2),
        "stamp_duty": round(stamp, 2),
        "gst":        round(gst, 2),
        "total_fees": total,
    }


def get_atm_strike(price: float, width: int) -> int:
    return int(math.floor(price / width) * width)


# ── Simulate Trade ────────────────────────────────────────────────────────────
def simulate_trade(inst_name: str, inst: dict, price_data: dict,
                   trend_info: dict) -> dict:
    """Simulate one trade based on detected trend."""
    today    = datetime.date.today()
    expiry   = get_expiry(inst_name, today)
    dte      = (expiry - today).days
    lot_size = inst["lot_size"]
    width    = inst["strike_width"]
    budget   = inst["budget"]
    strategy = trend_info["strategy"]   # 'BULL_CALL' or 'BEAR_PUT'
    spot     = price_data["current"]
    prev     = price_data["prev_close"]

    atm      = get_atm_strike(spot, width)

    if strategy == "BULL_CALL":
        buy_strike     = atm
        sell_strike    = atm + width
        option_type    = "CE"
        strategy_label = "Bull Call Spread"
    else:
        buy_strike     = atm + width   # buy higher put (ATM)
        sell_strike    = atm           # sell lower put (OTM)
        option_type    = "PE"
        strategy_label = "Bear Put Spread"

    # Premium based on DTE
    if dte <= 2:
        entry_pct = random.uniform(0.15, 0.25)
    elif dte <= 7:
        entry_pct = random.uniform(0.25, 0.40)
    elif dte <= 15:
        entry_pct = random.uniform(0.35, 0.50)
    else:
        entry_pct = random.uniform(0.40, 0.55)

    entry_premium = round(width * entry_pct, 2)

    # Dynamic lots — use full budget
    fees_est      = inst["brokerage"] * 2 + (inst["stt"] / 100 * entry_premium * lot_size)
    cost_per_lot  = (entry_premium * lot_size) + fees_est
    lots          = max(1, int(budget / cost_per_lot))
    units         = lots * lot_size
    total_cost    = round(entry_premium * units, 2)

    # Target and stop loss
    max_profit_pu       = width - entry_premium
    max_profit_total    = round(max_profit_pu * units, 2)
    target_profit       = round(max_profit_total * TARGET_PCT, 2)
    stop_loss_amt       = round(entry_premium * SL_PCT * units, 2)
    target_exit_premium = round(entry_premium + max_profit_pu * TARGET_PCT, 2)
    sl_exit_premium     = round(entry_premium * (1 - SL_PCT), 2)

    # Simulate outcome
    # Trend-confirmed trades have slightly higher win rate (60%)
    rand = random.random()
    if rand < 0.60:
        outcome      = "PROFIT"
        exit_premium = target_exit_premium
        exit_mins    = random.randint(20, 180)
    elif rand < 0.85:
        outcome      = "STOP LOSS"
        exit_premium = sl_exit_premium
        exit_mins    = random.randint(10, 120)
    else:
        outcome      = "FLAT"
        exit_premium = round(entry_premium * random.uniform(0.95, 1.05), 2)
        exit_mins    = random.randint(240, 360)

    gross_pnl    = round((exit_premium - entry_premium) * units, 2)
    fees         = calc_india_fees(inst, lots, entry_premium)
    net_pnl      = round(gross_pnl - fees["total_fees"], 2)
    capital_used = round(total_cost + fees["total_fees"], 2)

    now_ist  = datetime.datetime.now(IST)
    entry_t  = now_ist.replace(hour=9, minute=20, second=0, microsecond=0)
    exit_t   = entry_t + datetime.timedelta(minutes=exit_mins)

    return {
        "date":           now_ist.strftime("%Y-%m-%d"),
        "instrument":     inst_name,
        "strategy":       strategy_label,
        "trend":          trend_info["trend"],
        "trend_reason":   trend_info["reason"],
        "ema9":           trend_info["ema9"],
        "ema21":          trend_info["ema21"],
        "expiry":         expiry.strftime("%Y-%m-%d"),
        "expiry_type":    inst["expiry_type"].upper(),
        "dte":            dte,
        "entry_time":     entry_t.strftime("%H:%M IST"),
        "exit_time":      exit_t.strftime("%H:%M IST"),
        "spot_at_entry":  spot,
        "prev_close":     prev,
        "buy_strike":     f"{buy_strike} {option_type}",
        "sell_strike":    f"{sell_strike} {option_type}",
        "lots":           lots,
        "lot_size":       lot_size,
        "units":          units,
        "entry_premium":  entry_premium,
        "exit_premium":   exit_premium,
        "total_cost":     total_cost,
        "capital_used":   capital_used,
        "max_profit":     max_profit_total,
        "target_profit":  target_profit,
        "stop_loss_amt":  stop_loss_amt,
        "gross_pnl":      gross_pnl,
        "brokerage":      fees["brokerage"],
        "stt":            fees["stt"],
        "sebi_fee":       fees["sebi_fee"],
        "stamp_duty":     fees["stamp_duty"],
        "gst":            fees["gst"],
        "total_fees":     fees["total_fees"],
        "net_pnl":        net_pnl,
        "outcome":        outcome,
        "budget":         budget,
        "mode":           "PAPER TRADING",
    }


# ── Excel Setup ───────────────────────────────────────────────────────────────
HEADER_FILL = PatternFill("solid", start_color="1F4E79")
PROFIT_FILL = PatternFill("solid", start_color="C6EFCE")
LOSS_FILL   = PatternFill("solid", start_color="FFC7CE")
FLAT_FILL   = PatternFill("solid", start_color="FFEB9C")
SKIP_FILL   = PatternFill("solid", start_color="E2EFDA")
WHITE_FILL  = PatternFill("solid", start_color="FFFFFF")
SUBHDR_FILL = PatternFill("solid", start_color="D6E4F0")
THIN = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"),  bottom=Side(style="thin"),
)

INDIA_TRADE_COLS = [
    ("Date",             "date",           13),
    ("Instrument",       "instrument",     13),
    ("Strategy",         "strategy",       22),
    ("Trend",            "trend",          11),
    ("Trend Reason",     "trend_reason",   35),
    ("EMA 9",            "ema9",           10),
    ("EMA 21",           "ema21",          10),
    ("Expiry",           "expiry",         13),
    ("Expiry Type",      "expiry_type",    12),
    ("DTE",              "dte",             7),
    ("Entry Time",       "entry_time",     12),
    ("Exit Time",        "exit_time",      12),
    ("Spot at Entry",    "spot_at_entry",  15),
    ("Prev Close",       "prev_close",     12),
    ("Buy Strike",       "buy_strike",     14),
    ("Sell Strike",      "sell_strike",    14),
    ("Lots",             "lots",            8),
    ("Lot Size",         "lot_size",        9),
    ("Units",            "units",           9),
    ("Entry Premium",    "entry_premium",  15),
    ("Exit Premium",     "exit_premium",   14),
    ("Total Cost (₹)",   "total_cost",     14),
    ("Capital Used (₹)", "capital_used",   15),
    ("Max Profit (₹)",   "max_profit",     14),
    ("Target (₹)",       "target_profit",  13),
    ("Stop Loss (₹)",    "stop_loss_amt",  13),
    ("Gross P&L (₹)",    "gross_pnl",      13),
    ("Brokerage (₹)",    "brokerage",      13),
    ("STT (₹)",          "stt",            10),
    ("SEBI Fee (₹)",     "sebi_fee",       12),
    ("Stamp Duty (₹)",   "stamp_duty",     13),
    ("GST (₹)",          "gst",            10),
    ("Total Fees (₹)",   "total_fees",     13),
    ("Net P&L (₹)",      "net_pnl",        13),
    ("Outcome",          "outcome",        12),
    ("Budget (₹)",       "budget",         12),
    ("Mode",             "mode",           22),
]

SKIP_COLS = [
    ("Date",        12), ("Instrument",   13),
    ("Trend",       11), ("Trend Reason", 45),
    ("EMA 9",       10), ("EMA 21",       10),
    ("Action",      18),
]


def _hdr_cell(ws, row, col, text):
    c = ws.cell(row=row, column=col, value=text)
    c.font      = Font(bold=True, color="FFFFFF", name="Arial", size=10)
    c.fill      = HEADER_FILL
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c.border    = THIN
    return c


def _data_cell(ws, row, col, value, outcome=None):
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Arial", size=10)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.border    = THIN
    if outcome == "PROFIT":
        c.fill = PROFIT_FILL
    elif outcome == "STOP LOSS":
        c.fill = LOSS_FILL
    elif outcome == "FLAT":
        c.fill = FLAT_FILL
    elif outcome == "SKIP":
        c.fill = SKIP_FILL
    else:
        c.fill = WHITE_FILL
    return c


def build_india_sheet(wb, sheet_name: str, label: str):
    if sheet_name in wb.sheetnames:
        return
    ws = wb.create_sheet(sheet_name)
    ws.merge_cells(f"A1:{get_column_letter(len(INDIA_TRADE_COLS))}1")
    title        = ws["A1"]
    title.value  = f"{label} — Smart Trend-Based Paper Trading Journal"
    title.font   = Font(bold=True, color="FFFFFF", name="Arial", size=13)
    title.fill   = PatternFill("solid", start_color="0D2137")
    title.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28
    for i, (hdr, _, width) in enumerate(INDIA_TRADE_COLS, 1):
        _hdr_cell(ws, 2, i, hdr)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[2].height = 35
    ws.freeze_panes = "A3"


def build_skip_sheet(wb):
    """Sheet to log days when trend was conflicting and no trade was placed."""
    name = "Skipped_Days"
    if name in wb.sheetnames:
        return
    ws = wb.create_sheet(name)
    ws.merge_cells(f"A1:{get_column_letter(len(SKIP_COLS))}1")
    title       = ws["A1"]
    title.value = "Skipped Trading Days — Conflicting Trend Signals"
    title.font  = Font(bold=True, color="FFFFFF", name="Arial", size=13)
    title.fill  = PatternFill("solid", start_color="0D2137")
    title.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28
    for i, (hdr, width) in enumerate(SKIP_COLS, 1):
        _hdr_cell(ws, 2, i, hdr)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A3"


def append_india_trade(wb, trade: dict):
    sheet_name = f"{trade['instrument']}_Trades"
    if sheet_name not in wb.sheetnames:
        build_india_sheet(wb, sheet_name, trade["instrument"])
    ws       = wb[sheet_name]
    next_row = ws.max_row + 1
    outcome  = trade["outcome"]
    for i, (_, key, _) in enumerate(INDIA_TRADE_COLS, 1):
        _data_cell(ws, next_row, i, trade[key], outcome)


def append_skip_row(wb, inst_name: str, trend_info: dict):
    build_skip_sheet(wb)
    ws       = wb["Skipped_Days"]
    next_row = ws.max_row + 1
    now      = datetime.datetime.now(IST)
    values   = [
        now.strftime("%Y-%m-%d"),
        inst_name,
        trend_info["trend"],
        trend_info["reason"],
        trend_info["ema9"],
        trend_info["ema21"],
        "NO TRADE — Conflicting signals",
    ]
    for col, val in enumerate(values, 1):
        _data_cell(ws, next_row, col, val, "SKIP")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("  NIFTY / BANKNIFTY SMART TREND BOT — STARTING")
    log.info("=" * 65)

    today = datetime.date.today()
    if not is_india_market_day(today):
        log.info(f"{today} is not an Indian market trading day. Exiting.")
        return

    if not os.path.exists(JOURNAL_FILE):
        log.error("Journal not found. Run trading_bot.py first.")
        return

    wb = openpyxl.load_workbook(JOURNAL_FILE)
    for inst_name in ["NIFTY", "BANKNIFTY"]:
        build_india_sheet(wb, f"{inst_name}_Trades", inst_name)
    build_skip_sheet(wb)

    all_trades = []

    for inst_name, inst in INSTRUMENTS.items():
        log.info(f"\n{'─'*55}")
        log.info(f"  {inst_name} — Trend Analysis")
        log.info(f"{'─'*55}")

        price_data = fetch_price_history(inst["ticker"])

        # Fallback simulated data if fetch fails
        if price_data is None:
            log.warning(f"  Price fetch failed — using simulated data")
            if inst_name == "NIFTY":
                closes = [23000 + random.uniform(-200, 200) for _ in range(25)]
            else:
                closes = [50000 + random.uniform(-500, 500) for _ in range(25)]
            price_data = {
                "current":    round(closes[-1], 2),
                "prev_close": round(closes[-2], 2),
                "closes":     closes,
            }

        log.info(f"  Current    : {price_data['current']:,.2f}")
        log.info(f"  Prev Close : {price_data['prev_close']:,.2f}")

        trend_info = detect_trend(price_data)

        log.info(f"  Trend      : {trend_info['trend']}")
        log.info(f"  Reason     : {trend_info['reason']}")

        expiry = get_expiry(inst_name, today)
        dte    = (expiry - today).days
        log.info(f"  Expiry     : {expiry}  (DTE: {dte} days)")

        if trend_info["strategy"] is None:
            log.info(f"  ⚠️  SKIPPING {inst_name} today — conflicting signals")
            append_skip_row(wb, inst_name, trend_info)
            continue

        log.info(f"  Strategy   : {'Bull Call Spread' if trend_info['strategy']=='BULL_CALL' else 'Bear Put Spread'}")

        trade = simulate_trade(inst_name, inst, price_data, trend_info)
        all_trades.append(trade)
        append_india_trade(wb, trade)

        log.info(f"\n  Buy        : {trade['buy_strike']}")
        log.info(f"  Sell       : {trade['sell_strike']}")
        log.info(f"  Lots       : {trade['lots']}  ({trade['units']} units)")
        log.info(f"  Entry Prem : ₹{trade['entry_premium']:,.2f}")
        log.info(f"  Target     : ₹{trade['target_profit']:,.2f}")
        log.info(f"  Stop Loss  : ₹{trade['stop_loss_amt']:,.2f}")
        log.info(f"  Gross P&L  : ₹{trade['gross_pnl']:,.2f}")
        log.info(f"  Total Fees : ₹{trade['total_fees']:,.2f}")
        log.info(f"  Net P&L    : ₹{trade['net_pnl']:,.2f}")
        log.info(f"  Outcome    : {trade['outcome']}")

    wb.save(JOURNAL_FILE)
    log.info(f"\n✅ Journal updated → {JOURNAL_FILE}")

    # Summary
    log.info("\n" + "=" * 65)
    log.info("  INDIA TRADE SUMMARY")
    log.info("=" * 65)
    if all_trades:
        log.info(f"  {'Instrument':<13} {'Strategy':<22} {'Outcome':<12} {'Net P&L':>12}")
        log.info(f"  {'─'*60}")
        total_net = total_fees = 0
        for t in all_trades:
            log.info(f"  {t['instrument']:<13} {t['strategy']:<22} {t['outcome']:<12} ₹{t['net_pnl']:>10,.2f}")
            total_net  += t["net_pnl"]
            total_fees += t["total_fees"]
        log.info(f"  {'─'*60}")
        log.info(f"  {'TOTAL':<36} ₹{total_net:>10,.2f}  (Fees: ₹{total_fees:,.2f})")
    else:
        log.info("  No trades placed today — conflicting signals on all instruments.")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
