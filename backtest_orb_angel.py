"""
Strategy Two — Opening Range Breakout (ORB) Backtest
======================================================
Instrument  : NIFTY Futures
Data source : Angel One SmartAPI (real historical candle data)

RULES (as confirmed with user):
  - 9:00 AM - 9:30 AM   : No trading. Market settling.
  - 9:30 AM - 10:00 AM  : Observation window. Mark highest high and
                           lowest low of NIFTY Futures during this 30 min.
  - From 10:01 AM       : Entry window opens.
      - If price moves ABOVE the 9:30-10:00 high  -> go LONG
      - If price moves BELOW the 9:30-10:00 low   -> go SHORT
      - Triggers the instant the level is crossed (checked candle by
        candle on 5-min bars — see LIMITATION note below)
  - Only ONE trade per day (whichever direction breaks first)
  - Target     : +30 points from entry
  - Stop Loss  : -25 points from entry
  - If neither hit by market close -> force exit at 3:25 PM (5 min
    buffer before NSE F&O close at 3:30 PM)
  - Position size : 1 lot = 65 units (current NIFTY lot size, Jan 2026 revision)

LIMITATION — IMPORTANT TO UNDERSTAND:
  This backtest uses 5-MINUTE candles. The exact intra-candle moment of
  breakout, target-hit, or stop-hit is NOT known — only what happened by
  each 5-min candle's close/high/low. If BOTH target and stop-loss are
  touched within the SAME candle, we cannot know which happened first.
  This script is conservative: it assumes STOP-LOSS hits first in any
  ambiguous candle (worst-case), and flags this clearly in the trade log.

DATA AVAILABILITY NOTE:
  NIFTY Futures roll over monthly and EXPIRED contracts are often removed
  from Angel One's searchable scrip master entirely. This means a "2 month
  backtest" may only actually return data for the CURRENT (and sometimes
  immediately prior) contract, not contracts from 2 months ago. This script
  fetches whatever contracts ARE available and clearly reports which months
  were and were not covered, rather than silently producing a shorter
  backtest than requested.
"""

import datetime
import logging
import os
import sys
import time

import pandas as pd

from angel_one_client import AngelOneClient, _request

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Strategy Parameters ────────────────────────────────────────────────────
RANGE_START       = datetime.time(9, 30)
RANGE_END         = datetime.time(10, 0)
ENTRY_START       = datetime.time(10, 1)
FORCE_EXIT_TIME   = datetime.time(15, 25)
TARGET_POINTS     = 30
STOPLOSS_POINTS   = 25
LOT_SIZE          = 65
BACKTEST_DAYS     = 60


def last_tuesday(year: int, month: int) -> datetime.date:
    if month == 12:
        last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    while last_day.weekday() != 1:
        last_day -= datetime.timedelta(days=1)
    return last_day


def get_contract_months(start_date: datetime.date, end_date: datetime.date) -> list:
    months = []
    d = datetime.date(start_date.year, start_date.month, 1)
    while d <= end_date:
        months.append((d.year, d.month))
        if d.month == 12:
            d = datetime.date(d.year + 1, 1, 1)
        else:
            d = datetime.date(d.year, d.month + 1, 1)
    return months


def build_contract_symbol(year: int, month: int) -> str:
    expiry = last_tuesday(year, month)
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}FUT"


def fetch_candles_direct(client: AngelOneClient, exchange: str, symbol_token: str,
                         interval: str, from_date: str, to_date: str) -> list | None:
    """
    Bypass the existing get_candle_data()'s 'days back from now' design and
    call the API directly with an explicit date range — needed for backtesting
    historical periods rather than just 'recent N days'.
    """
    body = {
        "exchange":    exchange,
        "symboltoken": symbol_token,
        "interval":    interval,
        "fromdate":    from_date,
        "todate":      to_date,
    }
    resp = _request("POST", "/rest/secure/angelbroking/historical/v1/getCandleData",
                    client._headers(auth=True), body)
    if not resp.get("status"):
        log.warning(f"  Candle fetch failed: {resp.get('message')}")
        return None
    return resp.get("data")


def fetch_contract_candles(client: AngelOneClient, symbol: str,
                           from_date: datetime.date, to_date: datetime.date) -> pd.DataFrame | None:
    log.info(f"Searching for contract symbol: {symbol}")
    results = client.search_scrip("NFO", symbol)
    if not results:
        log.warning(f"  Could not find token for {symbol} — likely expired/not in scrip "
                    f"master cache. This contract's data will be SKIPPED (not silently "
                    f"substituted with anything else).")
        return None

    token = results[0]["symboltoken"]
    log.info(f"  Found token: {token}")

    from_str = f"{from_date.strftime('%Y-%m-%d')} 09:00"
    to_str   = f"{to_date.strftime('%Y-%m-%d')} 15:30"

    candles = fetch_candles_direct(client, "NFO", token, "FIVE_MINUTE", from_str, to_str)

    if not candles:
        log.warning(f"  No candle data returned for {symbol} ({from_str} to {to_str})")
        return None

    df = pd.DataFrame(candles, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    df["contract"] = symbol
    log.info(f"  Got {len(df)} candles for {symbol} (range: {df.index.min()} to {df.index.max()})")
    return df


def fetch_all_candles(client: AngelOneClient, days: int = 60):
    """Returns (combined_dataframe, months_covered, months_missing)."""
    end_date   = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days)

    months = get_contract_months(start_date, end_date)
    log.info(f"Backtest window requested: {start_date} to {end_date}")
    log.info(f"Contract months needed: {months}")

    all_dfs = []
    months_covered = []
    months_missing = []

    for year, month in months:
        symbol = build_contract_symbol(year, month)
        df = fetch_contract_candles(client, symbol, start_date, end_date)
        if df is not None and not df.empty:
            all_dfs.append(df)
            months_covered.append(f"{year}-{month:02d} ({symbol})")
        else:
            months_missing.append(f"{year}-{month:02d} ({symbol})")
        time.sleep(1)

    if not all_dfs:
        raise RuntimeError(
            "Could not fetch ANY NIFTY futures candle data from Angel One for any "
            "contract month in the requested window. Check ANGEL_* env vars and "
            "VPS IP whitelist in your Angel One SmartAPI app settings."
        )

    combined = pd.concat(all_dfs).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined, months_covered, months_missing


def run_backtest(df: pd.DataFrame) -> list:
    df = df.copy()
    df["date"] = df.index.date
    df["time"] = df.index.time

    trades = []

    for trade_date, day_df in df.groupby("date"):
        day_df = day_df.sort_index()

        range_df = day_df[(day_df["time"] >= RANGE_START) & (day_df["time"] < RANGE_END)]
        if range_df.empty:
            continue

        range_high = range_df["High"].max()
        range_low  = range_df["Low"].min()

        entry_df = day_df[day_df["time"] >= ENTRY_START]
        if entry_df.empty:
            continue

        position      = None
        entry_price   = None
        entry_time    = None
        exit_price    = None
        exit_time     = None
        exit_reason   = None
        ambiguous_candle = False
        target = stop = None

        for ts, candle in entry_df.iterrows():
            t = candle["time"]

            if position is None:
                if candle["High"] > range_high:
                    position    = "LONG"
                    entry_price = range_high
                    entry_time  = ts
                    target      = entry_price + TARGET_POINTS
                    stop        = entry_price - STOPLOSS_POINTS
                    continue
                elif candle["Low"] < range_low:
                    position    = "SHORT"
                    entry_price = range_low
                    entry_time  = ts
                    target      = entry_price - TARGET_POINTS
                    stop        = entry_price + STOPLOSS_POINTS
                    continue
                else:
                    continue
            else:
                if position == "LONG":
                    hit_target = candle["High"] >= target
                    hit_stop   = candle["Low"]  <= stop
                else:
                    hit_target = candle["Low"]  <= target
                    hit_stop   = candle["High"] >= stop

                if hit_target and hit_stop:
                    exit_price       = stop
                    exit_time        = ts
                    exit_reason      = "STOP LOSS (ambiguous candle)"
                    ambiguous_candle = True
                    break
                elif hit_target:
                    exit_price  = target
                    exit_time   = ts
                    exit_reason = "TARGET"
                    break
                elif hit_stop:
                    exit_price  = stop
                    exit_time   = ts
                    exit_reason = "STOP LOSS"
                    break
                elif t >= FORCE_EXIT_TIME:
                    exit_price  = candle["Close"]
                    exit_time   = ts
                    exit_reason = "FORCE EXIT (EOD)"
                    break

        if position is not None and exit_price is None:
            last_candle = entry_df.iloc[-1]
            exit_price  = last_candle["Close"]
            exit_time   = entry_df.index[-1]
            exit_reason = "FORCE EXIT (no further data)"

        if position is not None:
            if position == "LONG":
                points_pnl = round(exit_price - entry_price, 2)
            else:
                points_pnl = round(entry_price - exit_price, 2)
            rupee_pnl = round(points_pnl * LOT_SIZE, 2)

            trades.append({
                "date":        str(trade_date),
                "range_high":  round(range_high, 2),
                "range_low":   round(range_low, 2),
                "direction":   position,
                "entry_time":  entry_time.strftime("%H:%M"),
                "entry_price": round(entry_price, 2),
                "exit_time":   exit_time.strftime("%H:%M"),
                "exit_price":  round(exit_price, 2),
                "exit_reason": exit_reason,
                "points_pnl":  points_pnl,
                "rupee_pnl":   rupee_pnl,
                "ambiguous":   ambiguous_candle,
            })
        else:
            trades.append({
                "date":        str(trade_date),
                "range_high":  round(range_high, 2),
                "range_low":   round(range_low, 2),
                "direction":   "NO TRADE",
                "entry_time":  "-",
                "entry_price": "-",
                "exit_time":   "-",
                "exit_price":  "-",
                "exit_reason": "No breakout occurred",
                "points_pnl":  0,
                "rupee_pnl":   0,
                "ambiguous":   False,
            })

    return trades


def print_summary(trades: list, months_covered: list, months_missing: list):
    print("\n" + "=" * 95)
    print("  STRATEGY TWO — ORB BACKTEST RESULTS (NIFTY Futures, via Angel One)")
    print("=" * 95)

    print(f"\nContract months ACTUALLY covered : {months_covered}")
    if months_missing:
        print(f"Contract months MISSING (no data): {months_missing}")
        print(f"  ⚠️  This means the backtest does NOT cover the full requested period.")
        print(f"  ⚠️  Results below only reflect the months that had available data.")

    print(f"\n{'Date':<12}{'Dir':<10}{'Entry':<9}{'EntryT':<8}{'Exit':<9}{'ExitT':<8}{'Reason':<28}{'Pts':<8}{'Rs.':<10}")
    print("-" * 95)

    total_points = 0
    total_rupees = 0
    wins = losses = no_trades = ambiguous_count = 0

    for t in trades:
        print(f"{t['date']:<12}{t['direction']:<10}{str(t['entry_price']):<9}{t['entry_time']:<8}"
              f"{str(t['exit_price']):<9}{t['exit_time']:<8}{t['exit_reason']:<28}"
              f"{t['points_pnl']:<8}{t['rupee_pnl']:<10}")

        if t["direction"] == "NO TRADE":
            no_trades += 1
        else:
            total_points += t["points_pnl"]
            total_rupees += t["rupee_pnl"]
            if t["points_pnl"] > 0:
                wins += 1
            else:
                losses += 1
            if t["ambiguous"]:
                ambiguous_count += 1

    traded_days = wins + losses
    win_rate = (wins / traded_days * 100) if traded_days else 0

    print("-" * 95)
    print(f"\nTotal trading days analysed : {len(trades)}")
    print(f"Days with NO breakout       : {no_trades}")
    print(f"Days WITH a trade           : {traded_days}")
    print(f"  Wins                      : {wins}")
    print(f"  Losses                    : {losses}")
    print(f"  Win rate                  : {win_rate:.1f}%")
    print(f"  Ambiguous candles (worst-case SL assumed) : {ambiguous_count}")
    print(f"\nTotal Points P&L            : {total_points:+.2f}")
    print(f"Total Rupee P&L (1 lot={LOT_SIZE}) : Rs.{total_rupees:+,.2f}")
    print("=" * 95)


def main():
    log.info("Logging into Angel One...")
    client = AngelOneClient()
    if not client.login():
        log.error("❌ Could not login to Angel One. Check credentials/IP whitelist.")
        sys.exit(1)

    try:
        df, months_covered, months_missing = fetch_all_candles(client, days=BACKTEST_DAYS)
    except Exception as e:
        log.error(f"❌ DATA FETCH FAILED: {e}")
        sys.exit(1)

    log.info(f"\nTotal candles fetched: {len(df)}")
    log.info(f"Date range: {df.index.min()} to {df.index.max()}")

    trades = run_backtest(df)
    print_summary(trades, months_covered, months_missing)

    out_df = pd.DataFrame(trades)
    out_df.to_csv("strategy_two_backtest_results.csv", index=False)
    log.info(f"\n✅ Detailed results saved to strategy_two_backtest_results.csv")


if __name__ == "__main__":
    main()
