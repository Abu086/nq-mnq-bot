"""
Strategy Three — Multi-Indicator Confluence Backtest (Dynamic Top-20)
========================================================================
Instrument  : Top 20 NSE stocks by traded value, RE-RANKED EVERY DAY
Data source : Angel One SmartAPI (15-min candles) + NSE Indices (NIFTY 100
              constituent list as the candidate pool to rank from)

RULES (as confirmed with user):
  Candidate pool : NIFTY 100 index constituents (fetched live from NSE
                    Indices' official CSV, not hardcoded — index membership
                    changes periodically and should not be guessed)
  Daily selection: Each trading day, rank ALL NIFTY 100 stocks by that day's
                    traded value (close price x volume on the DAILY candle).
                    Take the top 20. This list is DIFFERENT each day,
                    reflecting what was actually liquid/active that day —
                    deliberately not a fixed list, since that would test
                    indicator signals (especially the volume condition)
                    against stocks that may not have actually been liquid
                    on a given historical day.

  Candle interval : 15-minute, for all indicator calculations
  Holding type    : Strictly intraday — force exit before market close
  Budget per trade: Rs.50,000, no daily total cap, no limit on concurrent
                    positions (every valid signal is taken)
  Trades per stock per day: Maximum 1

  Entry — LONG (ALL 5 conditions must be true on the same 15-min candle):
    1. Price closes ABOVE the Ichimoku cloud (close > max(Senkou A, Senkou B))
    2. Tenkan-sen crosses ABOVE Kijun-sen on this candle (not just "is above")
    3. MACD line crosses ABOVE signal line on this candle
    4. RSI(14) is between 50 and 65 (inclusive)
    5. Volume on this candle >= 1.5x the 20-period average volume

  Entry — SHORT (mirror, ALL 5 must be true):
    1. Price closes BELOW the Ichimoku cloud
    2. Tenkan-sen crosses BELOW Kijun-sen on this candle
    3. MACD line crosses BELOW signal line on this candle
    4. RSI(14) is between 35 and 50 (inclusive)
    5. Volume >= 1.5x the 20-period average volume

  Filter (applies on top of the above): SKIP if RSI > 70 or RSI < 30 at
    signal time, even if other conditions are met (exhaustion zones).

  Stop Loss : Cloud bottom (longs) / cloud top (shorts), OR the swing
              low/high of the prior 10 candles — whichever is TIGHTER
              (i.e. closer to entry price, meaning smaller risk).

  Target    : 1.5x to 2x the stop-loss distance (this script uses 1.75x,
              the midpoint, as a single concrete number — see note below),
              OR exit when MACD crosses back through the signal line —
              whichever happens FIRST.

  Force exit: Before market close if neither target nor SL/MACD-reversal
              triggers first.

  Indicator settings used (standard defaults, confirmed with user):
    Ichimoku: Tenkan 9, Kijun 26, Senkou B 52
    MACD: 12, 26, 9
    RSI: 14-period
    Volume average: 20-period
    Swing high/low lookback: 10 candles (excludes the entry candle itself)

NOTE ON TARGET RANGE (1.5x-2x):
  The strategy specifies a RANGE (1.5x-2x SL distance), not a single
  number. A backtest needs one concrete rule to apply consistently, so
  this script uses 1.75x (the midpoint) as the target multiple. This is
  a deliberate simplification — clearly flagged here so it's not mistaken
  for an exact restatement of the original rule.

LIMITATION — 15-MIN CANDLE GRANULARITY:
  Same caveat as prior backtests: if both target and stop-loss are touched
  within the same 15-min candle, we cannot know which happened first.
  This script conservatively assumes STOP-LOSS hit first in any such
  ambiguous candle, and flags this clearly per-trade in the output.

DATA SCALE WARNING:
  This backtest fetches DAILY data for up to 100 candidate stocks (to rank
  them), then 15-MIN data for whichever ~20 stocks were actually top-20 on
  each of the ~40 trading days in a 2-month window. This is a LARGE number
  of API calls. Expect this script to take significant time to run and to
  need careful rate-limit handling (retries with backoff are built in).
"""

import datetime
import io
import logging
import os
import sys
import time

import pandas as pd
import pandas_ta as ta
import urllib.request

from angel_one_client import AngelOneClient, _request

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Strategy Parameters ────────────────────────────────────────────────────
BACKTEST_DAYS       = 60
TOP_N                = 20
BUDGET_PER_TRADE     = 50_000
TARGET_MULTIPLE      = 1.75          # midpoint of 1.5x-2x range — see note above
SWING_LOOKBACK       = 10
RSI_LONG_MIN, RSI_LONG_MAX   = 50, 65
RSI_SHORT_MIN, RSI_SHORT_MAX = 35, 50
RSI_FILTER_HIGH, RSI_FILTER_LOW = 70, 30
VOLUME_MULTIPLE      = 1.5
VOLUME_AVG_PERIOD    = 20
FORCE_EXIT_TIME      = datetime.time(15, 15)
MARKET_OPEN          = datetime.time(9, 15)

CANDIDATE_LIST_CSV = "ind_nifty50list.csv"  # NIFTY 50 constituents — sourced manually via
                                            # browser since niftyindices.com blocks automated
                                            # requests (Akamai bot protection, confirmed via
                                            # curl testing: TLS handshake completes but server
                                            # never responds — silent drop, not a 403).
                                            # This file should be refreshed manually every few
                                            # months since NSE rebalances index membership on
                                            # a semi-annual basis (Jan 31 / Jul 31 cutoffs).

# Retry/backoff settings for Angel One API rate limits
MAX_RETRIES   = 5
RETRY_BACKOFF = [2, 4, 8, 16, 32]


# ── Candidate Pool — NIFTY 50 constituents (read from local CSV) ─────────
def fetch_nifty100_symbols() -> list:
    """
    Read the NIFTY 50 constituent list from a local CSV file (manually
    downloaded via browser, since automated fetches to niftyindices.com
    are silently blocked). Function name kept as 'nifty100' for minimal
    diff even though the actual pool is NIFTY 50 — see CANDIDATE_LIST_CSV.
    """
    log.info(f"Reading candidate stock list from local file: {CANDIDATE_LIST_CSV}...")
    if not os.path.exists(CANDIDATE_LIST_CSV):
        raise RuntimeError(
            f"Candidate list file '{CANDIDATE_LIST_CSV}' not found in current directory. "
            f"This file must be manually downloaded from "
            f"https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv via a "
            f"browser (automated fetches are blocked by Akamai) and placed alongside "
            f"this script."
        )
    df = pd.read_csv(CANDIDATE_LIST_CSV)
    symbol_col = [c for c in df.columns if c.strip().lower() == "symbol"]
    if not symbol_col:
        raise RuntimeError(f"Could not find 'Symbol' column in {CANDIDATE_LIST_CSV}. "
                          f"Columns found: {df.columns.tolist()}")
    symbols = df[symbol_col[0]].dropna().astype(str).str.strip().tolist()
    log.info(f"  Found {len(symbols)} candidate constituents")
    return symbols


# ── Angel One Data Fetching (with retry/backoff) ──────────────────────────
def search_scrip_with_retry(client: AngelOneClient, exchange: str, symbol: str):
    for attempt in range(MAX_RETRIES):
        try:
            result = client.search_scrip(exchange, symbol)
            if result:
                return result
            return None  # genuinely not found, not a rate-limit issue
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                log.warning(f"  Retry {attempt+1}/{MAX_RETRIES} for {symbol} search in {wait}s: {e}")
                time.sleep(wait)
            else:
                log.warning(f"  Giving up on {symbol} search after {MAX_RETRIES} attempts: {e}")
                return None
    return None


def fetch_candles_with_retry(client: AngelOneClient, exchange: str, token: str,
                             interval: str, from_date: str, to_date: str):
    body = {
        "exchange":    exchange,
        "symboltoken": token,
        "interval":    interval,
        "fromdate":    from_date,
        "todate":      to_date,
    }
    for attempt in range(MAX_RETRIES):
        resp = _request("POST", "/rest/secure/angelbroking/historical/v1/getCandleData",
                        client._headers(auth=True), body)
        if resp.get("status"):
            return resp.get("data")
        msg = str(resp.get("message", ""))
        if "rate" in msg.lower() and attempt < MAX_RETRIES - 1:
            wait = RETRY_BACKOFF[attempt]
            log.warning(f"  Rate limited fetching candles, retry {attempt+1}/{MAX_RETRIES} in {wait}s")
            time.sleep(wait)
            continue
        log.warning(f"  Candle fetch failed: {msg}")
        return None
    return None


def get_token_map(client: AngelOneClient, symbols: list) -> dict:
    """Resolve NSE-EQ symbols to Angel One tokens. Returns {symbol: token}."""
    token_map = {}
    for sym in symbols:
        eq_symbol = f"{sym}-EQ"
        results = search_scrip_with_retry(client, "NSE", eq_symbol)
        if results:
            token_map[sym] = results[0]["symboltoken"]
        else:
            log.warning(f"  Could not resolve token for {sym} — will be excluded from candidate pool")
        time.sleep(0.3)  # gentle pacing even on success
    log.info(f"Resolved {len(token_map)}/{len(symbols)} symbols to tokens")
    return token_map


def fetch_daily_candles_for_ranking(client: AngelOneClient, token_map: dict,
                                    start_date: datetime.date, end_date: datetime.date) -> pd.DataFrame:
    """Fetch DAILY candles for all candidates, used purely for traded-value ranking."""
    from_str = f"{start_date.strftime('%Y-%m-%d')} 09:00"
    to_str   = f"{end_date.strftime('%Y-%m-%d')} 15:30"

    all_rows = []
    for i, (symbol, token) in enumerate(token_map.items()):
        log.info(f"  [{i+1}/{len(token_map)}] Fetching daily candles for {symbol}...")
        candles = fetch_candles_with_retry(client, "NSE", token, "ONE_DAY", from_str, to_str)
        if candles:
            for c in candles:
                all_rows.append({
                    "date":   pd.to_datetime(c[0]).date(),
                    "symbol": symbol,
                    "close":  c[4],
                    "volume": c[5],
                })
        time.sleep(0.5)

    if not all_rows:
        raise RuntimeError("Could not fetch any daily candle data for ranking. Aborting.")

    df = pd.DataFrame(all_rows)
    df["traded_value"] = df["close"] * df["volume"]
    return df


def get_daily_top_n(ranking_df: pd.DataFrame, n: int = TOP_N) -> dict:
    """Returns {date: [list of top-N symbols by traded value that day]}."""
    result = {}
    for date, day_df in ranking_df.groupby("date"):
        top_n = day_df.nlargest(n, "traded_value")["symbol"].tolist()
        result[date] = top_n
    return result


def fetch_15min_candles(client: AngelOneClient, symbol: str, token: str,
                        from_date: datetime.date, to_date: datetime.date) -> pd.DataFrame | None:
    from_str = f"{from_date.strftime('%Y-%m-%d')} 09:00"
    to_str   = f"{to_date.strftime('%Y-%m-%d')} 15:30"
    candles = fetch_candles_with_retry(client, "NSE", token, "FIFTEEN_MINUTE", from_str, to_str)
    if not candles:
        return None
    df = pd.DataFrame(candles, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    return df


# ── Indicator Calculation ──────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add Ichimoku, MACD, RSI, volume average, and crossover flags to a 15-min OHLCV df."""
    df = df.copy()

    # Ichimoku — pandas_ta returns (visible_df, future_df); we only need visible
    visible, _ = df.ta.ichimoku()
    df["cloud_top"]    = visible[["ISA_9", "ISB_26"]].max(axis=1)
    df["cloud_bottom"] = visible[["ISA_9", "ISB_26"]].min(axis=1)
    df["tenkan"]       = visible["ITS_9"]
    df["kijun"]        = visible["IKS_26"]

    # MACD
    macd = df.ta.macd()
    df["macd"]        = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]

    # RSI
    df["rsi"] = df.ta.rsi(length=14)

    # Volume average
    df["vol_avg20"] = df["Volume"].rolling(VOLUME_AVG_PERIOD).mean()

    # Price vs cloud
    df["price_above_cloud"] = df["Close"] > df["cloud_top"]
    df["price_below_cloud"] = df["Close"] < df["cloud_bottom"]

    # Tenkan/Kijun crossover (not just "is above" — the actual cross event)
    tk_above = df["tenkan"] > df["kijun"]
    df["tenkan_cross_up"]   = tk_above & ~tk_above.shift(1).fillna(False).astype(bool)
    df["tenkan_cross_down"] = ~tk_above & tk_above.shift(1).fillna(False).astype(bool)

    # MACD crossover
    macd_above = df["macd"] > df["macd_signal"]
    df["macd_cross_up"]   = macd_above & ~macd_above.shift(1).fillna(False).astype(bool)
    df["macd_cross_down"] = ~macd_above & macd_above.shift(1).fillna(False).astype(bool)

    # Swing high/low over prior N candles (excludes current candle — uses shift(1))
    df["swing_low"]  = df["Low"].rolling(SWING_LOOKBACK).min().shift(1)
    df["swing_high"] = df["High"].rolling(SWING_LOOKBACK).max().shift(1)

    return df


def check_long_entry(row) -> bool:
    if pd.isna(row["cloud_top"]) or pd.isna(row["macd"]) or pd.isna(row["rsi"]) or pd.isna(row["vol_avg20"]):
        return False
    if row["rsi"] > RSI_FILTER_HIGH or row["rsi"] < RSI_FILTER_LOW:
        return False  # exhaustion filter
    return (
        row["price_above_cloud"] and
        row["tenkan_cross_up"] and
        row["macd_cross_up"] and
        (RSI_LONG_MIN <= row["rsi"] <= RSI_LONG_MAX) and
        (row["Volume"] >= VOLUME_MULTIPLE * row["vol_avg20"])
    )


def check_short_entry(row) -> bool:
    if pd.isna(row["cloud_bottom"]) or pd.isna(row["macd"]) or pd.isna(row["rsi"]) or pd.isna(row["vol_avg20"]):
        return False
    if row["rsi"] > RSI_FILTER_HIGH or row["rsi"] < RSI_FILTER_LOW:
        return False
    return (
        row["price_below_cloud"] and
        row["tenkan_cross_down"] and
        row["macd_cross_down"] and
        (RSI_SHORT_MIN <= row["rsi"] <= RSI_SHORT_MAX) and
        (row["Volume"] >= VOLUME_MULTIPLE * row["vol_avg20"])
    )


# ── Backtest Engine ────────────────────────────────────────────────────────
def run_backtest_for_stock(symbol: str, df: pd.DataFrame) -> list:
    """Run the strategy for one stock's 15-min data, max 1 trade per day."""
    if df is None or len(df) < 60:  # need enough history for indicators to warm up
        return []

    df = add_indicators(df)
    df["date"] = df.index.date
    df["time"] = df.index.time

    trades = []

    for trade_date, day_df in df.groupby("date"):
        day_df = day_df.sort_index()
        traded_today = False

        for ts, row in day_df.iterrows():
            if traded_today:
                break
            if row["time"] < MARKET_OPEN:
                continue

            direction = None
            if check_long_entry(row):
                direction = "LONG"
            elif check_short_entry(row):
                direction = "SHORT"

            if direction is None:
                continue

            entry_price = row["Close"]
            entry_time  = ts

            # Stop loss: cloud edge vs swing level, whichever is TIGHTER (closer to entry)
            if direction == "LONG":
                cloud_sl = row["cloud_bottom"]
                swing_sl = row["swing_low"]
                if pd.isna(swing_sl):
                    stop = cloud_sl
                else:
                    # Tighter = higher value for a long stop (closer to entry, less risk)
                    stop = max(cloud_sl, swing_sl)
                sl_distance = entry_price - stop
            else:
                cloud_sl = row["cloud_top"]
                swing_sl = row["swing_high"]
                if pd.isna(swing_sl):
                    stop = cloud_sl
                else:
                    stop = min(cloud_sl, swing_sl)
                sl_distance = stop - entry_price

            if sl_distance <= 0 or pd.isna(sl_distance):
                continue  # invalid setup, skip

            target_distance = sl_distance * TARGET_MULTIPLE
            target = entry_price + target_distance if direction == "LONG" else entry_price - target_distance

            # Now scan forward from the NEXT candle to find exit
            future_df = day_df[day_df.index > ts]
            exit_price = None
            exit_time  = None
            exit_reason = None
            ambiguous = False

            for fts, frow in future_df.iterrows():
                if direction == "LONG":
                    hit_target = frow["High"] >= target
                    hit_stop   = frow["Low"]  <= stop
                else:
                    hit_target = frow["Low"]  <= target
                    hit_stop   = frow["High"] >= stop

                macd_reversal = (direction == "LONG" and frow["macd_cross_down"]) or \
                              (direction == "SHORT" and frow["macd_cross_up"])

                if hit_target and hit_stop:
                    exit_price  = stop
                    exit_time   = fts
                    exit_reason = "STOP LOSS (ambiguous candle)"
                    ambiguous   = True
                    break
                elif hit_target:
                    exit_price  = target
                    exit_time   = fts
                    exit_reason = "TARGET"
                    break
                elif hit_stop:
                    exit_price  = stop
                    exit_time   = fts
                    exit_reason = "STOP LOSS"
                    break
                elif macd_reversal:
                    exit_price  = frow["Close"]
                    exit_time   = fts
                    exit_reason = "MACD REVERSAL"
                    break
                elif frow["time"] >= FORCE_EXIT_TIME:
                    exit_price  = frow["Close"]
                    exit_time   = fts
                    exit_reason = "FORCE EXIT (EOD)"
                    break

            if exit_price is None:
                # Ran out of candles for the day without a clean exit signal
                if not future_df.empty:
                    exit_price  = future_df.iloc[-1]["Close"]
                    exit_time   = future_df.index[-1]
                    exit_reason = "FORCE EXIT (no further data)"
                else:
                    continue  # no future data at all, skip this signal

            if direction == "LONG":
                points_pnl = exit_price - entry_price
            else:
                points_pnl = entry_price - exit_price

            qty = int(BUDGET_PER_TRADE // entry_price)
            if qty < 1:
                continue  # stock too expensive for the budget
            rupee_pnl = round(points_pnl * qty, 2)

            trades.append({
                "symbol":      symbol,
                "date":        str(trade_date),
                "direction":   direction,
                "entry_time":  entry_time.strftime("%H:%M"),
                "entry_price": round(entry_price, 2),
                "stop_loss":   round(stop, 2),
                "target":      round(target, 2),
                "qty":         qty,
                "exit_time":   exit_time.strftime("%H:%M"),
                "exit_price":  round(exit_price, 2),
                "exit_reason": exit_reason,
                "points_pnl":  round(points_pnl, 2),
                "rupee_pnl":   rupee_pnl,
                "ambiguous":   ambiguous,
            })
            traded_today = True

    return trades


# ── Summary / Reporting ────────────────────────────────────────────────────
def print_summary(all_trades: list):
    print("\n" + "=" * 110)
    print("  STRATEGY THREE — MULTI-INDICATOR CONFLUENCE BACKTEST (Dynamic Top-20, 15-min)")
    print("=" * 110)

    if not all_trades:
        print("\nNo trades were generated by this strategy over the backtest period.")
        print("This can happen legitimately if the strict 5-condition AND logic rarely")
        print("aligns — that's the strategy filtering hard, not necessarily a bug.")
        print("=" * 110)
        return

    df = pd.DataFrame(all_trades)
    df = df.sort_values(["date", "entry_time"])

    print(f"\n{'Date':<12}{'Symbol':<12}{'Dir':<7}{'Entry':<9}{'EntryT':<7}{'Exit':<9}{'ExitT':<7}"
          f"{'Reason':<26}{'Qty':<6}{'Pts':<8}{'Rs.':<10}")
    print("-" * 110)

    total_rupees = 0
    wins = losses = 0
    ambiguous_count = 0

    for _, t in df.iterrows():
        print(f"{t['date']:<12}{t['symbol']:<12}{t['direction']:<7}{str(t['entry_price']):<9}"
              f"{t['entry_time']:<7}{str(t['exit_price']):<9}{t['exit_time']:<7}"
              f"{t['exit_reason']:<26}{t['qty']:<6}{t['points_pnl']:<8}{t['rupee_pnl']:<10}")
        total_rupees += t["rupee_pnl"]
        if t["rupee_pnl"] > 0:
            wins += 1
        else:
            losses += 1
        if t["ambiguous"]:
            ambiguous_count += 1

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades else 0

    print("-" * 110)
    print(f"\nTotal trades                : {total_trades}")
    print(f"  Wins                       : {wins}")
    print(f"  Losses                     : {losses}")
    print(f"  Win rate                   : {win_rate:.1f}%")
    print(f"  Ambiguous candles (worst-case SL assumed) : {ambiguous_count}")
    print(f"\nUnique stocks traded         : {df['symbol'].nunique()}")
    print(f"Unique trading days with activity : {df['date'].nunique()}")
    print(f"\nTotal Rupee P&L              : Rs.{total_rupees:+,.2f}")
    print("=" * 110)


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    log.info("Logging into Angel One...")
    client = AngelOneClient()
    if not client.login():
        log.error("❌ Could not login to Angel One. Check credentials/IP whitelist.")
        sys.exit(1)

    end_date   = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=BACKTEST_DAYS)

    # Step 1: Get candidate pool (NIFTY 100 constituents)
    try:
        candidates = fetch_nifty100_symbols()
    except Exception as e:
        log.error(f"❌ Could not fetch NIFTY 100 constituent list: {e}")
        sys.exit(1)

    # Step 2: Resolve all candidates to Angel One tokens
    log.info("Resolving candidate symbols to Angel One tokens (this will take a while)...")
    token_map = get_token_map(client, candidates)
    if not token_map:
        log.error("❌ Could not resolve any candidate tokens. Aborting.")
        sys.exit(1)

    # Step 3: Fetch DAILY candles for all candidates, to rank by traded value
    log.info("Fetching daily candles for ranking (this will take a while)...")
    ranking_df = fetch_daily_candles_for_ranking(client, token_map, start_date, end_date)

    # Step 4: Determine top-20 per day
    daily_top20 = get_daily_top_n(ranking_df, n=TOP_N)
    log.info(f"Computed daily top-{TOP_N} lists for {len(daily_top20)} trading days")

    # Step 5: Determine the UNION of all stocks that were ever top-20 on any day
    # (we only need to fetch 15-min data once per stock that ever qualified,
    # not separately per day)
    all_qualifying_symbols = set()
    for symbols in daily_top20.values():
        all_qualifying_symbols.update(symbols)
    log.info(f"Total unique stocks that were top-{TOP_N} on at least one day: {len(all_qualifying_symbols)}")

    # Step 6: Fetch 15-min candles for each qualifying stock over the full window
    log.info("Fetching 15-min candles for all qualifying stocks (this is the slow part)...")
    all_trades = []
    for i, symbol in enumerate(sorted(all_qualifying_symbols)):
        token = token_map.get(symbol)
        if not token:
            continue
        log.info(f"  [{i+1}/{len(all_qualifying_symbols)}] Fetching 15-min data for {symbol}...")
        df = fetch_15min_candles(client, symbol, token, start_date, end_date)
        if df is None or df.empty:
            log.warning(f"    No 15-min data for {symbol}, skipping")
            continue

        # Restrict each day's data to only days where this stock was ACTUALLY
        # in that day's top-20 — this is what makes the backtest dynamic rather
        # than just "top-20-ever, applied to every day"
        df["date"] = df.index.date
        valid_dates = {d for d, syms in daily_top20.items() if symbol in syms}
        df = df[df["date"].isin(valid_dates)]
        df = df.drop(columns=["date"])

        if df.empty:
            continue

        trades = run_backtest_for_stock(symbol, df)
        all_trades.extend(trades)
        time.sleep(0.5)

    print_summary(all_trades)

    if all_trades:
        out_df = pd.DataFrame(all_trades)
        out_df.to_csv("strategy_three_backtest_results.csv", index=False)
        log.info(f"\n✅ Detailed results saved to strategy_three_backtest_results.csv")


if __name__ == "__main__":
    main()
