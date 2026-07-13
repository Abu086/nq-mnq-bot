"""
Main Scheduler v2 — Strategy Four ONLY
========================================
Runs only Strategy Four Live Breakout Bot.
Bull/Bear spreads are PAUSED (code kept, not scheduled).
Paper trading bots are REMOVED.
"""

import datetime
import logging
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

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

# ── Kill switches ──────────────────────────────────────────────────────────
STRATEGY_FOUR_ENABLED = True   # Set False to pause Strategy Four
# BULL_SPREAD_ENABLED = False  # Paused
# BEAR_SPREAD_ENABLED = False  # Paused

INDIA_HOLIDAYS_2026 = {
    datetime.date(2026,  1, 26), datetime.date(2026,  2, 26),
    datetime.date(2026,  3, 17), datetime.date(2026,  4,  2),
    datetime.date(2026,  4, 14), datetime.date(2026,  4, 30),
    datetime.date(2026,  8, 15), datetime.date(2026,  8, 27),
    datetime.date(2026, 10,  2), datetime.date(2026, 10, 20),
    datetime.date(2026, 11,  9), datetime.date(2026, 11, 10),
    datetime.date(2026, 11, 25), datetime.date(2026, 12, 25),
}

def is_india_market_day(dt: datetime.date) -> bool:
    return dt.weekday() < 5 and dt not in INDIA_HOLIDAYS_2026

def run_background(script: str) -> bool:
    """Launch a script in background — non-blocking."""
    # Safety: kill any stray/leftover process of this script before launching
    try:
        subprocess.run(["pkill", "-9", "-f", script], check=False)
        time.sleep(2)
    except Exception:
        pass
    log.info(f"▶ Launching {script} in background...")
    try:
        proc = subprocess.Popen(
            [sys.executable, script],
            stdout=open(f"logs/{script.replace('.py','')}_out.log", "a"),
            stderr=subprocess.STDOUT,
        )
        log.info(f"✅ {script} launched (PID {proc.pid})")
        return True
    except Exception as e:
        log.error(f"❌ Failed to launch {script}: {e}")
        return False

def main():
    log.info("=" * 60)
    log.info("  TRADING BOT SCHEDULER v2 — STRATEGY FOUR ONLY")
    log.info("  Bull/Bear Spreads: PAUSED")
    log.info("  Paper Trading: REMOVED")
    log.info("=" * 60)

    ran_today = {"strategy_four": None, "universe_update": None}

    while True:
        now_ist = datetime.datetime.now(IST)
        today   = now_ist.date()

        # Reset at midnight
        if now_ist.hour == 0 and now_ist.minute == 0:
            ran_today["strategy_four"]   = None
            ran_today["universe_update"] = None

        # ── 11:20 AM IST — Strategy Four Live Bot ─────────────────────────
        if (STRATEGY_FOUR_ENABLED and
                now_ist.hour > 11 or (now_ist.hour == 11 and now_ist.minute >= 20)) and (
                now_ist.hour < 15 and
                ran_today["strategy_four"] != today and
                is_india_market_day(today)):

            log.info(f"\n{'='*50}")
            log.info(f"  Launching Strategy Four Live Breakout Bot")
            log.info(f"{'='*50}")
            if run_background("live_breakout_bot.py"):
                ran_today["strategy_four"] = today
            else:
                log.warning("  Strategy Four failed to launch — will retry in 30s")

        # ── 4:00 PM IST — Universe Updater (after market close) ──────────
        # Downloads latest NIFTY 500 list, ranks by today's traded value,
        # saves top-100 cache for tomorrow — runs every trading day
        if (now_ist.hour == 16 and now_ist.minute >= 0 and
                ran_today["universe_update"] != today and
                is_india_market_day(today)):

            log.info(f"\n{'='*50}")
            log.info(f"  4:00 PM IST — Running Universe Updater")
            log.info(f"{'='*50}")
            if run_background("update_universe.py"):
                ran_today["universe_update"] = today

        time.sleep(30)

if __name__ == "__main__":
    main()
