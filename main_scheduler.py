"""
Main Scheduler — Runs ALL bots on schedule
==========================================
Runs continuously on Hostinger VPS (Mumbai)
Schedule:
  9:20 AM IST  → Paper trading bots (NIFTY + BANKNIFTY all 4 strategies)
  9:20 AM IST  → Live NIFTY Bull Call Spread (Tuesdays only, bullish trend)
  10:00 PM IST → NQ/MNQ paper trading (10:00 AM ET = 10:30 PM IST)
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

# Track what ran today to avoid double-runs
ran_today = {
    "india_paper": None,
    "india_live":  None,
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

def run_script(script: str):
    log.info(f"▶ Running {script}...")
    result = subprocess.run(
        [sys.executable, script],
        capture_output=False,
        text=True,
    )
    if result.returncode == 0:
        log.info(f"✅ {script} completed successfully")
    else:
        log.error(f"❌ {script} failed with code {result.returncode}")

def main():
    log.info("=" * 60)
    log.info("  TRADING BOT SCHEDULER — STARTING")
    log.info("  Running on Hostinger VPS Mumbai")
    log.info("=" * 60)

    while True:
        now_ist = datetime.datetime.now(IST)
        now_et  = datetime.datetime.now(ET)
        today   = now_ist.date()

        # Reset daily trackers at midnight
        if now_ist.hour == 0 and now_ist.minute == 0:
            ran_today["india_paper"] = None
            ran_today["india_live"]  = None
            ran_today["us_paper"]    = None

        # ── 9:20 AM IST — India Paper + Live Bots ────────────────────────────
        if (now_ist.hour == 9 and now_ist.minute >= 20 and
                ran_today["india_paper"] != today and
                is_india_market_day(today)):

            log.info(f"\n{'='*50}")
            log.info(f"  9:20 AM IST — Running India bots")
            log.info(f"{'='*50}")
            run_script("india_bot.py")
            ran_today["india_paper"] = today

        # ── 9:20 AM IST — Live NIFTY Bot (Mon/Wed/Thu/Fri — not expiry day) ──
        if (now_ist.hour == 9 and now_ist.minute >= 20 and
                ran_today["india_live"] != today and
                is_india_market_day(today) and
                today.weekday() != 1):   # Not Tuesday (expiry day)

            log.info(f"\n{'='*50}")
            log.info(f"  9:20 AM IST — Running LIVE NIFTY bot")
            log.info(f"{'='*50}")
            run_script("live_nifty_bot.py")
            ran_today["india_live"] = today

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
