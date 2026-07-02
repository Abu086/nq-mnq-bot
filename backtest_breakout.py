"""
Strategy Four — N-Candle High/Low Breakout Backtest
======================================================
Instrument  : Top 100 NSE stocks by traded value, re-ranked EVERY DAY
Data source : Angel One SmartAPI (15-min candles)

RULES (confirmed with user):
  Candidate pool  : NIFTY 100 constituents (ind_nifty100list.csv — manually
                    downloaded via browser since niftyindices.com blocks
                    automated/datacenter requests via Akamai bot protection)
  Daily selection : Each trading day, rank ALL NIFTY 100 stocks by that
                    day's traded value (close × volume on the daily candle).
                    Take the top 100 — i.e. ALL of them since the candidate
                    pool IS 100 stocks. This still applies dynamic ranking
                    so the actual traded-value order is known each day.
                    NOTE: user said "top 100" and candidate pool is NIFTY 100,
                    so effectively all candidates qualify each day — but the
                    daily ranking is still computed so we can filter or extend
                    to a larger pool later without code changes.

  Candle interval : 15-minute, strictly intraday
  Lookback        : 10 candles (= 150 minutes = 2.5 hours of prior price action)
  Budget per trade: Rs.50,000, no daily cap
  Trades per day  : Multiple allowed — as many stocks as signal — but
                    MAXIMUM 1 TRADE PER STOCK PER DAY

  Entry — LONG:
    Current candle's HIGH breaks above the highest HIGH of the prior
    10 candles (not including the current candle).
    Entry price = the breakout level (prior 10-candle high).

  Entry — SHORT:
    Current candle's LOW breaks below the lowest LOW of the prior
    10 candles.
    Entry price = the breakout level (prior 10-candle low).

  Stop Loss : 0.5% from entry price
    Long  → stop = entry × (1 - 0.005)
    Short → stop = entry × (1 + 0.005)

  Target : 1.0% from entry price  (2:1 reward/risk ratio)
    Long  → target = entry × (1 + 0.010)
    Short → target = entry × (1 - 0.010)

  Force exit: 3:15 PM IST if neither target nor SL hit

  Backtest period : Last 2 months (60 calendar days)

LIMITATION — 15-MIN CANDLE GRANULARITY:
  If both target and stop-loss are touched within the same 15-min candle,
  we cannot know which happened first. This script conservatively assumes
  STOP-LOSS hit first in any ambiguous candle (worst-case assumption),
  and flags this clearly per-trade.
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
BACKTEST_DAYS    = 60
CANDIDATE_CSV    = "ind_nifty500list.csv"  # NIFTY 500 — covers ~94% of NSE market cap
LOOKBACK         = 5           # prior candles — signals from 10:30 AM (5 x 15min from 9:15)
BUDGET           = 5_000       # Rs.5,000 own capital per trade (MIS 5x margin = Rs.25,000 effective)
SL_PCT           = 0.005       # 0.5%
TARGET_PCT       = 0.020       # 2.0% (4:1 reward/risk ratio)
FORCE_EXIT_TIME  = datetime.time(15, 15)
MARKET_OPEN      = datetime.time(9, 15)
LAST_ENTRY_TIME  = datetime.time(13, 15)  # Filter 5: No entries after 1:15 PM — ensures 2hrs runway for 2% target

# Angel One rate-limit retry settings
MAX_RETRIES   = 5
RETRY_BACKOFF = [2, 4, 8, 16, 32]


# ── Candidate Pool ────────────────────────────────────────────────────────
def load_candidates() -> list:
    """Load NIFTY 100 constituent symbols from the manually-downloaded CSV."""
    if not os.path.exists(CANDIDATE_CSV):
        raise RuntimeError(
            f"'{CANDIDATE_CSV}' not found in current directory. "
            f"Download it via browser from: "
            f"https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"
        )
    df = pd.read_csv(CANDIDATE_CSV)
    col = [c for c in df.columns if c.strip().lower() == "symbol"]
    if not col:
        raise RuntimeError(f"No 'Symbol' column found in {CANDIDATE_CSV}. "
                          f"Columns: {df.columns.tolist()}")
    symbols = df[col[0]].dropna().astype(str).str.strip().tolist()
    log.info(f"Loaded {len(symbols)} candidate symbols from {CANDIDATE_CSV}")
    return symbols


# ── Angel One API Helpers (with retry/backoff) ────────────────────────────
def search_with_retry(client: AngelOneClient, exchange: str, symbol: str):
    for attempt in range(MAX_RETRIES):
        try:
            result = client.search_scrip(exchange, symbol)
            if result:
                return result
            return None
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                log.warning(f"  Retry {attempt+1}/{MAX_RETRIES} searching {symbol} in {wait}s: {e}")
                time.sleep(wait)
            else:
                log.warning(f"  Giving up on {symbol}: {e}")
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
            log.warning(f"  Rate limited, retry {attempt+1}/{MAX_RETRIES} in {wait}s")
            time.sleep(wait)
            continue
        log.warning(f"  Candle fetch failed: {msg}")
        return None
    return None


def resolve_tokens(client: AngelOneClient, symbols: list) -> dict:
    """Returns {symbol: token} for all resolvable symbols."""
    token_map = {}
    for i, sym in enumerate(symbols):
        result = search_with_retry(client, "NSE", f"{sym}-EQ")
        if result:
            token_map[sym] = result[0]["symboltoken"]
        else:
            log.warning(f"  [{i+1}/{len(symbols)}] Could not resolve token for {sym}")
        time.sleep(1.0)
    log.info(f"Resolved {len(token_map)}/{len(symbols)} tokens")
    return token_map


def fetch_daily_for_ranking(client: AngelOneClient, token_map: dict,
                            start_date: datetime.date,
                            end_date: datetime.date) -> pd.DataFrame:
    """Fetch ONE_DAY candles for all candidates to compute daily traded value."""
    from_str = f"{start_date.strftime('%Y-%m-%d')} 09:00"
    to_str   = f"{end_date.strftime('%Y-%m-%d')} 15:30"
    rows = []
    for i, (sym, token) in enumerate(token_map.items()):
        log.info(f"  [{i+1}/{len(token_map)}] Daily candles for {sym}...")
        candles = fetch_candles(client, "NSE", token, "ONE_DAY", from_str, to_str)
        if candles:
            for c in candles:
                rows.append({
                    "date":   pd.to_datetime(c[0]).date(),
                    "symbol": sym,
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                })
        time.sleep(0.4)
    if not rows:
        raise RuntimeError("No daily candle data fetched for ranking.")
    df = pd.DataFrame(rows)
    df["traded_value"] = df["close"] * df["volume"]
    return df


def get_daily_top_n(ranking_df: pd.DataFrame, n: int) -> dict:
    """
    Returns {trade_date: [top-N symbols by previous day's traded value]}.

    Key design: on any given trade_date, we select stocks based on the
    PREVIOUS trading day's traded value — not today's. This avoids
    look-ahead bias (you can't know today's traded value at market open)
    and exactly matches what a live bot would do: rank yesterday's data
    overnight, then trade that list today.
    """
    # Get sorted list of all dates in the ranking data
    all_dates = sorted(ranking_df["date"].unique())

    # Build top-N per date
    top_per_date = {}
    for date, day_df in ranking_df.groupby("date"):
        top_per_date[date] = day_df.nlargest(n, "traded_value")["symbol"].tolist()

    # Shift: trade_date uses PREVIOUS day's ranking
    result = {}
    for i, trade_date in enumerate(all_dates):
        if i == 0:
            continue  # No previous day available for the first date — skip it
        prev_date = all_dates[i - 1]
        result[trade_date] = top_per_date[prev_date]

    return result


def fetch_15min(client: AngelOneClient, symbol: str, token: str,
                start_date: datetime.date,
                end_date: datetime.date) -> pd.DataFrame | None:
    from_str = f"{start_date.strftime('%Y-%m-%d')} 09:00"
    to_str   = f"{end_date.strftime('%Y-%m-%d')} 15:30"
    candles  = fetch_candles(client, "NSE", token, "FIFTEEN_MINUTE", from_str, to_str)
    if not candles:
        return None
    df = pd.DataFrame(candles,
                      columns=["timestamp","Open","High","Low","Close","Volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    return df


# ── Backtest Engine ────────────────────────────────────────────────────────
def backtest_stock(symbol: str, df: pd.DataFrame,
                   valid_dates: set) -> list:
    """
    Run breakout strategy on one stock's 15-min data.
    Only processes days where this stock was actually in the top-N.

    OPENING GAP FIX: Prior high/low is computed using ONLY today's candles,
    not yesterday's. This means no signals can fire until at least LOOKBACK
    candles have formed today (earliest signal = 11:30 AM IST, after 10 x
    15-min candles from 9:15 AM). This eliminates the false 9:15 AM signals
    caused by overnight gaps breaking yesterday's price range.
    """
    if df is None or len(df) < LOOKBACK + 2:
        return []

    df = df.copy()
    df["date"] = df.index.date
    df["time"] = df.index.time

    trades = []

    for trade_date, day_df in df.groupby("date"):
        if trade_date not in valid_dates:
            continue

        day_df = day_df.sort_index().copy()

        # Compute rolling indicators using ONLY THIS DAY's candles
        day_df["prior_high"] = day_df["High"].rolling(LOOKBACK).max().shift(1)
        day_df["prior_low"]  = day_df["Low"].rolling(LOOKBACK).min().shift(1)
        day_df["vol_avg20"]  = day_df["Volume"].rolling(20).mean().shift(1)
        day_df["ma20"]       = day_df["Close"].rolling(20).mean().shift(1)
        # Range width of prior LOOKBACK candles (for tight consolidation filter)
        day_df["range_width_pct"] = (
            (day_df["High"].rolling(LOOKBACK).max() - day_df["Low"].rolling(LOOKBACK).min())
            / day_df["prior_high"] * 100
        ).shift(1)

        traded_today = False

        for ts, row in day_df.iterrows():
            if traded_today:
                break
            if row["time"] < MARKET_OPEN:
                continue
            if row["time"] > LAST_ENTRY_TIME:
                continue  # Filter 5: No entries after 1:15 PM — need 2hrs runway for 2% target
            if pd.isna(row["prior_high"]) or pd.isna(row["prior_low"]):
                continue
            if pd.isna(row["vol_avg20"]) or pd.isna(row["ma20"]):
                continue

            # Breakout detection
            direction = None
            if row["High"] > row["prior_high"]:
                direction   = "LONG"
                entry_price = row["prior_high"]
            elif row["Low"] < row["prior_low"]:
                direction   = "SHORT"
                entry_price = row["prior_low"]

            if direction is None:
                continue

            # ── HIGH CONVICTION FILTERS ───────────────────────────────────
            # Filter 1: Volume confirmation — breakout candle volume ≥ 1.5x avg
            if row["Volume"] < 1.5 * row["vol_avg20"]:
                continue

            # Filter 2: Breakout magnitude — price must break range by ≥ 0.3%
            if direction == "LONG":
                breakout_pct = (row["High"] - row["prior_high"]) / row["prior_high"] * 100
            else:
                breakout_pct = (row["prior_low"] - row["Low"]) / row["prior_low"] * 100
            if breakout_pct < 0.3:
                continue

            # Filter 3: Trend alignment — trade with the 20-candle MA direction
            if direction == "LONG" and row["Close"] < row["ma20"]:
                continue  # Price below MA — don't go long
            if direction == "SHORT" and row["Close"] > row["ma20"]:
                continue  # Price above MA — don't go short

            # Filter 4: Tight consolidation — prior range must be narrow (< 1%)
            if pd.isna(row["range_width_pct"]) or row["range_width_pct"] > 1.0:
                continue  # Wide, choppy range — skip
            # ── END FILTERS ───────────────────────────────────────────────

            # Stop loss and target
            if direction == "LONG":
                stop   = entry_price * (1 - SL_PCT)
                target = entry_price * (1 + TARGET_PCT)
            else:
                stop   = entry_price * (1 + SL_PCT)
                target = entry_price * (1 - TARGET_PCT)

            # Scan forward from next candle for exit
            future_df   = day_df[day_df.index > ts]
            exit_price  = None
            exit_time   = None
            exit_reason = None
            ambiguous   = False

            for fts, frow in future_df.iterrows():
                if direction == "LONG":
                    hit_target = frow["High"] >= target
                    hit_stop   = frow["Low"]  <= stop
                else:
                    hit_target = frow["Low"]  <= target
                    hit_stop   = frow["High"] >= stop

                if hit_target and hit_stop:
                    exit_price  = stop
                    exit_time   = fts
                    exit_reason = "STOP LOSS (ambiguous)"
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
                elif frow["time"] >= FORCE_EXIT_TIME:
                    exit_price  = frow["Close"]
                    exit_time   = fts
                    exit_reason = "FORCE EXIT (EOD)"
                    break

            if exit_price is None and not future_df.empty:
                exit_price  = future_df.iloc[-1]["Close"]
                exit_time   = future_df.index[-1]
                exit_reason = "FORCE EXIT (no further data)"

            if exit_price is None:
                continue

            # MIS intraday margin: Angel One gives ~5x leverage
            # So Rs.2,000 own capital = Rs.10,000 effective buying power
            MARGIN_MULTIPLE = 5
            effective_budget = BUDGET * MARGIN_MULTIPLE
            qty = max(1, int(effective_budget // entry_price))
            if direction == "LONG":
                pnl_pts = exit_price - entry_price
            else:
                pnl_pts = entry_price - exit_price
            pnl_rs = round(pnl_pts * qty, 2)

            trades.append({
                "symbol":      symbol,
                "date":        str(trade_date),
                "direction":   direction,
                "entry_time":  ts.strftime("%H:%M"),
                "entry_price": round(entry_price, 2),
                "stop":        round(stop, 2),
                "target":      round(target, 2),
                "qty":         qty,
                "exit_time":   exit_time.strftime("%H:%M"),
                "exit_price":  round(exit_price, 2),
                "exit_reason": exit_reason,
                "pnl_pts":     round(pnl_pts, 2),
                "pnl_rs":      pnl_rs,
                "ambiguous":   ambiguous,
            })
            traded_today = True

    return trades


# ── Summary ────────────────────────────────────────────────────────────────
def print_summary(all_trades: list):
    print("\n" + "=" * 115)
    print("  STRATEGY FOUR — 10-CANDLE BREAKOUT BACKTEST (NIFTY 100, 15-min, Dynamic Daily Top)")
    print("=" * 115)

    if not all_trades:
        print("\n  No trades generated over the backtest period.")
        print("=" * 115)
        return

    df = pd.DataFrame(all_trades).sort_values(["date", "entry_time"])

    print(f"\n{'Date':<12}{'Symbol':<14}{'Dir':<7}{'Entry':<10}{'ET':<7}"
          f"{'Exit':<10}{'XT':<7}{'Reason':<24}{'Qty':<6}{'Pts':<9}{'Rs.':<10}")
    print("-" * 115)

    total_rs = wins = losses = ambig = 0
    for _, t in df.iterrows():
        print(f"{t['date']:<12}{t['symbol']:<14}{t['direction']:<7}"
              f"{t['entry_price']:<10}{t['entry_time']:<7}"
              f"{t['exit_price']:<10}{t['exit_time']:<7}"
              f"{t['exit_reason']:<24}{t['qty']:<6}"
              f"{t['pnl_pts']:<9}{t['pnl_rs']:<10}")
        total_rs += t["pnl_rs"]
        if t["pnl_rs"] > 0:
            wins += 1
        else:
            losses += 1
        if t["ambiguous"]:
            ambig += 1

    n = wins + losses
    print("-" * 115)
    print(f"\nTotal trades          : {n}")
    print(f"  Wins                : {wins}  ({wins/n*100:.1f}%)" if n else "")
    print(f"  Losses              : {losses}  ({losses/n*100:.1f}%)" if n else "")
    print(f"  Ambiguous candles   : {ambig}")
    print(f"\nUnique stocks traded  : {df['symbol'].nunique()}")
    print(f"Active trading days   : {df['date'].nunique()}")
    print(f"\nTotal P&L             : Rs.{total_rs:+,.2f}")
    print("=" * 115)


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  STRATEGY FOUR — BREAKOUT BACKTEST STARTING")
    log.info("=" * 55)

    # Login
    client = AngelOneClient()
    if not client.login():
        log.error("Angel One login failed. Check credentials.")
        sys.exit(1)

    end_date   = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=BACKTEST_DAYS)
    log.info(f"Backtest window: {start_date} to {end_date}")

    # Load candidate pool
    candidates = load_candidates()

    # Resolve tokens
    log.info("Resolving symbols to Angel One tokens...")
    token_map = resolve_tokens(client, candidates)
    if not token_map:
        log.error("No tokens resolved. Aborting.")
        sys.exit(1)

    # Fetch daily candles for ranking
    log.info("Fetching daily candles for traded-value ranking...")
    ranking_df = fetch_daily_for_ranking(client, token_map, start_date, end_date)

    # Compute daily top-100 by traded value from the 500 candidate pool
    TOP_N = 100
    daily_top = get_daily_top_n(ranking_df, n=TOP_N)
    log.info(f"Daily rankings computed for {len(daily_top)} trading days")

    # Find all stocks that ever qualified
    qualifying = set()
    for syms in daily_top.values():
        qualifying.update(syms)
    log.info(f"Unique stocks to fetch 15-min data for: {len(qualifying)}")

    # Fetch 15-min data and run backtest per stock
    all_trades = []
    for i, symbol in enumerate(sorted(qualifying)):
        token = token_map.get(symbol)
        if not token:
            continue
        log.info(f"  [{i+1}/{len(qualifying)}] {symbol}...")
        df15 = fetch_15min(client, symbol, token, start_date, end_date)
        if df15 is None or df15.empty:
            log.warning(f"    No 15-min data for {symbol}, skipping")
            continue

        # Only process days where this stock was in the daily top list
        df15["date"] = df15.index.date
        valid_dates  = {d for d, syms in daily_top.items() if symbol in syms}
        df15 = df15[df15["date"].isin(valid_dates)].drop(columns=["date"])

        if df15.empty:
            continue

        trades = backtest_stock(symbol, df15, valid_dates)
        all_trades.extend(trades)
        time.sleep(0.4)

    print_summary(all_trades)

    # ── Rolling Capital Pool Simulation ──────────────────────────────────
    # Rules:
    #   - Total budget: Rs.84,000
    #   - Per trade:    Rs.5,000 own capital
    #   - Max simultaneous open positions: 84,000 ÷ 5,000 = 16
    #   - When a position closes (target/SL/EOD), that Rs.5,000 is freed
    #     and can be used for the next signal — capital recycles all day
    #   - First signal of each candle gets priority (sorted by entry_time)
    #   - Only 1 trade per stock per day (already enforced per stock above)

    DAILY_BUDGET      = 84_000
    CAPITAL_PER_TRADE = BUDGET       # Rs.5,000
    MAX_CONCURRENT    = int(DAILY_BUDGET // CAPITAL_PER_TRADE)  # 16

    log.info(f"\nApplying rolling capital pool:")
    log.info(f"  Total budget    : Rs.{DAILY_BUDGET:,}")
    log.info(f"  Per trade       : Rs.{CAPITAL_PER_TRADE:,}")
    log.info(f"  Max concurrent  : {MAX_CONCURRENT} positions")

    # Sort ALL trades by date + entry_time for chronological processing
    all_trades_df = pd.DataFrame(all_trades)
    if all_trades_df.empty:
        log.warning("No trades to apply capital limit to.")
        return

    all_trades_df = all_trades_df.sort_values(["date", "entry_time"]).reset_index(drop=True)

    filtered = []

    for trade_date, day_trades in all_trades_df.groupby("date"):
        day_trades = day_trades.sort_values("entry_time").reset_index(drop=True)

        # Track open positions as list of exit_times
        # Each entry in open_positions is the exit_time of an open trade
        open_positions = []  # list of exit_time strings for currently open trades

        for _, trade in day_trades.iterrows():
            entry_time = trade["entry_time"]
            exit_time  = trade["exit_time"]

            # Remove positions that have already closed before this entry
            open_positions = [
                ep for ep in open_positions
                if ep > entry_time  # still open at the time of this new signal
            ]

            # Check if we have capacity for a new position
            if len(open_positions) < MAX_CONCURRENT:
                filtered.append(trade.to_dict())
                open_positions.append(exit_time)  # reserve a slot until exit

    log.info(f"Trades before rolling capital: {len(all_trades)}")
    log.info(f"Trades after rolling capital : {len(filtered)}")

    print("\n\n" + "=" * 115)
    print(f"  AFTER ROLLING CAPITAL POOL (Rs.{DAILY_BUDGET:,} / Rs.{CAPITAL_PER_TRADE:,} = max {MAX_CONCURRENT} concurrent positions)")
    print("=" * 115)
    print_summary(filtered)

    if filtered:
        pd.DataFrame(filtered).to_csv(
            "strategy_four_backtest_results.csv", index=False)
        log.info("✅ Results saved to strategy_four_backtest_results.csv")


if __name__ == "__main__":
    main()
