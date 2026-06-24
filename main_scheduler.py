"""
Main Scheduler — Runs ALL bots on schedule
==========================================
Runs continuously on Hostinger VPS (Mumbai)
Schedule:
  9:20 AM IST  → Paper trading bots (NIFTY + BANKNIFTY all 4 strategies)
  10:00 AM ET  → NQ/MNQ paper trading

⚠️ LIVE NIFTY TRADING IS CURRENTLY DISABLED ⚠️
Disabled on 2026-06-24 after a real-money naked position incident caused by
Angel One's margin-benefit-on-hedge not behaving as expected via API, even
after sequencing BUY-then-confirmed-SELL. DO NOT re-enable until the margin
mechanism is verified directly with Angel One support or via the official
Margin Calculator API (/rest/secure/angelbroking/margin/v1/batch).
"""

import datetime
import logging
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ET  = ZoneInfo("America/New_York")

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/scheduler.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Live trading kill switch ──────────────────────────────────────────────────
LIVE_TRADING_ENABLED = False   # ⚠️ Set to True only after margin mechanism is verified

# Track what ran today to avoid double-runs
ran_today = {
    "india_paper": None,
    "us_paper":    None,
}

INDIA_HOLIDAYS_2026 = {
    datetime.date(2026,  1, 26), datetime.date(2026,  2, 26),
    datetime.date(2026,  3, 17), datetime.date(2026,  4,  2),
    datetime.date(2026,  4, 14), datetime.date(2026,  4, 30),
    datetime.date(2026,  8, 15), datetime.date(2026,  8, 27),
    datetime.date(2026, 10,  2), datetime.date(2026, 10, 20),
    datetime.date(2026, 11,  9), datetime.date(2026, 11, 10),
    datetime.date(2026, 11, 25), datetime.date(2026, 12, 25),
}

US_HOLIDAYS_2026 = {
    datetime.date(2026,  1,  1), datetime.date(2026,  1, 19),
    datetime.date(2026,  2, 16), datetime.date(2026,  4,  3),
    datetime.date(2026,  5, 25), datetime.date(2026,  7,  3),
    datetime.date(2026,  9,  7), datetime.date(2026, 11, 26),
    datetime.date(2026, 12, 25),
}

def is_india_market_day(dt: datetime.date) -> bool:
    return dt.weekday() < 5 and dt not in INDIA_HOLIDAYS_2026

def is_us_market_day(dt: datetime.date) -> bool:
    return dt.weekday() < 5 and dt not in US_HOLIDAYS_2026

def run_script(script: str) -> bool:
    """Run a script and return True only if it completed successfully (exit code 0)."""
    log.info(f"▶ Running {script}...")
    result = subprocess.run(
        [sys.executable, script],
        capture_output=False,
        text=True,
    )
    if result.returncode == 0:
        log.info(f"✅ {script} completed successfully")
        return True
    else:
        log.error(f"❌ {script} failed with code {result.returncode}")
        return False

def main():
    log.info("=" * 60)
    log.info("  TRADING BOT SCHEDULER — STARTING")
    log.info("  Running on Hostinger VPS Mumbai")
    if not LIVE_TRADING_ENABLED:
        log.info("  ⚠️  LIVE NIFTY TRADING IS DISABLED — paper trading only")
    log.info("=" * 60)

    while True:
        now_ist = datetime.datetime.now(IST)
        now_et  = datetime.datetime.now(ET)
        today   = now_ist.date()

        # Reset daily trackers at midnight
        if now_ist.hour == 0 and now_ist.minute == 0:
            ran_today["india_paper"] = None
            ran_today["us_paper"]    = None

        # ── 9:20 AM IST — India Paper Bot ONLY (live trading disabled) ───────
        if (now_ist.hour == 9 and now_ist.minute >= 20 and
                ran_today["india_paper"] != today and
                is_india_market_day(today)):

            log.info(f"\n{'='*50}")
            log.info(f"  9:20 AM IST — Running India paper trading bot")
            log.info(f"{'='*50}")
            run_script("india_bot.py")
            ran_today["india_paper"] = today

            if not LIVE_TRADING_ENABLED:
                log.info("  ⚠️  Skipping LIVE NIFTY bot — live trading currently disabled")

        # ── 10:00 AM ET — US Paper Bot ────────────────────────────────────────
        if (now_et.hour == 10 and now_et.minute >= 0 and
                ran_today["us_paper"] != today and
                is_us_market_day(now_et.date())):

            log.info(f"\n{'='*50}")
            log.info(f"  10:00 AM ET — Running US paper bot")
            log.info(f"{'='*50}")
            run_script("trading_bot.py")
            ran_today["us_paper"] = today

        # Sleep 30 seconds between checks
        time.sleep(30)

if __name__ == "__main__":
    main()
