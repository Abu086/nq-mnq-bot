"""
Universe Updater — runs automatically
======================================
1. Downloads latest NIFTY 500 list from niftyindices.com
2. Ranks all 500 stocks by previous day's traded value (Yahoo Finance)
3. Saves top-100 ranking cache for tomorrow's trading
4. Resolves any new stock tokens via Angel One API

Run this script:
- Every evening after market close to prepare next day's ranking
- On the 1st of each month to update the NIFTY 500 constituent list
"""

import datetime
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/universe_updater.log"),
    ],
)
log = logging.getLogger(__name__)

NIFTY500_CSV     = "ind_nifty500list.csv"
TOKEN_CACHE      = "nifty500_token_cache.json"
RANKING_CACHE    = "top100_ranking_cache.json"
NIFTY500_URL     = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"
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

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://niftyindices.com/",
}


def get_prev_trading_day() -> datetime.date:
    """Get the most recent trading day before today."""
    d = datetime.date.today() - datetime.timedelta(days=1)
    while d.weekday() >= 5 or d in INDIA_HOLIDAYS_2026:
        d -= datetime.timedelta(days=1)
    return d


def get_next_trading_day() -> datetime.date:
    """Get the next trading day after today."""
    d = datetime.date.today() + datetime.timedelta(days=1)
    while d.weekday() >= 5 or d in INDIA_HOLIDAYS_2026:
        d += datetime.timedelta(days=1)
    return d


# ── Step 1: Update NIFTY 500 constituent list ─────────────────────────────
def update_nifty500_list():
    log.info("Step 1: Updating NIFTY 500 constituent list from niftyindices.com...")
    try:
        req = urllib.request.Request(NIFTY500_URL, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read()
        with open(NIFTY500_CSV, "wb") as f:
            f.write(content)
        df = pd.read_csv(NIFTY500_CSV)
        col = [c for c in df.columns if c.strip().lower() == "symbol"][0]
        symbols = df[col].dropna().astype(str).str.strip().tolist()
        log.info(f"✅ Updated {NIFTY500_CSV} with {len(symbols)} stocks")
        return symbols
    except Exception as e:
        log.warning(f"⚠️ Could not update from web: {e} — using existing file")
        df = pd.read_csv(NIFTY500_CSV)
        col = [c for c in df.columns if c.strip().lower() == "symbol"][0]
        return df[col].dropna().astype(str).str.strip().tolist()


# ── Step 2: Resolve new tokens via Angel One ──────────────────────────────
def update_token_cache(symbols: list):
    log.info("Step 2: Checking for new tokens to resolve...")
    token_map = {}
    if os.path.exists(TOKEN_CACHE):
        with open(TOKEN_CACHE) as f:
            token_map = json.load(f)

    missing = [s for s in symbols if s not in token_map]
    if not missing:
        log.info(f"✅ All {len(symbols)} tokens already cached")
        return token_map

    log.info(f"Resolving {len(missing)} new tokens via Angel One...")
    try:
        from angel_one_client import AngelOneClient
        client = AngelOneClient()
        if not client.login():
            log.error("Angel One login failed — skipping token resolution")
            return token_map

        for i, sym in enumerate(missing):
            try:
                result = client.search_scrip("NSE", f"{sym}-EQ")
                if result:
                    token_map[sym] = result[0]["symboltoken"]
                    log.info(f"  [{i+1}/{len(missing)}] ✅ {sym}")
                else:
                    log.warning(f"  [{i+1}/{len(missing)}] ❌ {sym} not found")
            except Exception as e:
                log.warning(f"  [{i+1}/{len(missing)}] ⚠️ {sym}: {e}")
            time.sleep(1.5)

        with open(TOKEN_CACHE, "w") as f:
            json.dump(token_map, f)
        log.info(f"✅ Token cache updated: {len(token_map)} total")
    except Exception as e:
        log.error(f"Token resolution failed: {e}")

    return token_map


# ── Step 3: Rank by previous day's traded value via Yahoo Finance ─────────
def update_ranking_cache(symbols: list):
    log.info("Step 3: Ranking stocks by previous day's traded value (Yahoo Finance)...")
    prev_day = get_prev_trading_day()
    next_day = get_next_trading_day()
    date_str = prev_day.strftime("%Y-%m-%d")

    log.info(f"  Fetching {date_str} data for {len(symbols)} stocks...")
    rows = []
    failed = 0

    for i, sym in enumerate(symbols):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=5d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            result = data["chart"]["result"][0]
            timestamps = result["timestamp"]
            closes  = result["indicators"]["quote"][0]["close"]
            volumes = result["indicators"]["quote"][0]["volume"]
            for j, ts in enumerate(timestamps):
                import datetime as dt
                day = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                if day == date_str and closes[j] and volumes[j]:
                    rows.append({"symbol": sym, "traded_value": closes[j] * volumes[j]})
                    break
        except:
            failed += 1
        if (i + 1) % 100 == 0:
            log.info(f"  [{i+1}/{len(symbols)}] processed, {len(rows)} successful...")
        time.sleep(0.3)

    log.info(f"  Got data for {len(rows)} stocks, {failed} failed")

    if not rows:
        log.error("❌ No data fetched — ranking cache NOT updated")
        return

    CAUTIONARY_STOCKS = {"BIOCON", "TRENT", "ADANIENT", "INDUSINDBK", "ADANIGREEN"}
    df = pd.DataFrame(rows)
    df = df[~df["symbol"].isin(CAUTIONARY_STOCKS)]
    top100 = df.nlargest(TOP_N, "traded_value")["symbol"].tolist()

    cache = {"date": str(next_day), "symbols": top100}
    with open(RANKING_CACHE, "w") as f:
        json.dump(cache, f)

    log.info(f"✅ Ranking cache saved for {next_day}: {len(top100)} stocks")
    log.info(f"  Top 5: {top100[:5]}")


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  UNIVERSE UPDATER — STARTING")
    log.info("=" * 55)

    symbols = update_nifty500_list()
    update_token_cache(symbols)
    update_ranking_cache(symbols)

    log.info("\n✅ Universe update complete — ready for tomorrow's trading")


if __name__ == "__main__":
    main()
