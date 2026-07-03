"""
Main Scheduler — Runs ALL bots on schedule
==========================================
Runs continuously on Hostinger VPS (Mumbai)
Schedule:
  9:20 AM IST  → Paper trading bots (NIFTY + BANKNIFTY all 4 strategies)
  9:20 AM IST  → Live NIFTY Bull Call Spread (Mon/Wed/Thu/Fri, bullish trend only)
  10:00 AM ET  → NQ/MNQ paper trading
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

# ── Live trading kill switch ──────────────────────────────────────────────
LIVE_TRADING_ENABLED = True   # Set to False to disable live trading safely
STRATEGY_FOUR_ENABLED = True  # Set to False to disable Strategy Four breakout bot

# ── Daily run trackers (reset at midnight) ────────────────────────────────
ran_today = {
    "india_paper":   None,
    "india_live":    None,
    "india_bear":    None,
    "us_paper":      None,
    "strategy_four": None,
}

# Last attempt times for retry logic
last_live_attempt = None
last_bear_attempt = None
last_s4_attempt   = None
RETRY_COOLDOWN_MINUTES = 10

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
    """Run a script and WAIT for it to finish. Use for short scripts only."""
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

def run_script_background(script: str) -> bool:
    """
    Launch a script in the BACKGROUND (non-blocking).
    Use for long-running bots that monitor positions all day.
    Returns True immediately after launching — does not wait for completion.
    """
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
    global last_live_attempt, last_bear_attempt, last_s4_attempt

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

        # ── Reset daily trackers at midnight ─────────────────────────────
        if now_ist.hour == 0 and now_ist.minute == 0:
            ran_today["india_paper"]   = None
            ran_today["india_live"]    = None
            ran_today["india_bear"]    = None
            ran_today["us_paper"]      = None
            ran_today["strategy_four"] = None
            last_live_attempt          = None
            last_bear_attempt          = None
            last_s4_attempt            = None

        # ── 9:20 AM IST — India Paper Bot ────────────────────────────────
        if (now_ist.hour == 9 and now_ist.minute >= 20 and
                ran_today["india_paper"] != today and
                is_india_market_day(today)):

            log.info(f"\n{'='*50}")
            log.info(f"  9:20 AM IST — Running India paper trading bot")
            log.info(f"{'='*50}")
            run_script("india_bot.py")
            ran_today["india_paper"] = today

        # ── 9:20 AM IST onwards — Live NIFTY Bull Call Spread ────────────
        # Runs Mon/Wed/Thu/Fri (not Tuesday = expiry day)
        # Retries every 10 min until 3:00 PM if it errors
        if (LIVE_TRADING_ENABLED and
                now_ist.hour >= 9 and now_ist.hour < 15 and
                not (now_ist.hour == 9 and now_ist.minute < 20) and
                ran_today["india_live"] != today and
                is_india_market_day(today) and
                today.weekday() != 1 and
                (last_live_attempt is None or
                 (now_ist - last_live_attempt).total_seconds() >= RETRY_COOLDOWN_MINUTES * 60)):

            log.info(f"\n{'='*50}")
            log.info(f"  Attempting LIVE NIFTY Bull Call Spread")
            log.info(f"{'='*50}")
            last_live_attempt = now_ist
            success = run_script_background("live_nifty_bot.py")
            if success:
                ran_today["india_live"] = today
            else:
                log.warning(f"  ⚠️ Bull bot failed to launch — will retry in {RETRY_COOLDOWN_MINUTES} min")

        # ── 9:20 AM IST onwards — Live NIFTY Bear Put Spread ─────────────
        # Runs same days as bull bot — both check trend independently
        # Bull trades on bullish days, Bear trades on bearish days
        if (LIVE_TRADING_ENABLED and
                now_ist.hour >= 9 and now_ist.hour < 15 and
                not (now_ist.hour == 9 and now_ist.minute < 20) and
                ran_today["india_bear"] != today and
                is_india_market_day(today) and
                today.weekday() != 1 and
                (last_bear_attempt is None or
                 (now_ist - last_bear_attempt).total_seconds() >= RETRY_COOLDOWN_MINUTES * 60)):

            log.info(f"\n{'='*50}")
            log.info(f"  Attempting LIVE NIFTY Bear Put Spread")
            log.info(f"{'='*50}")
            last_bear_attempt = now_ist
            success = run_script_background("live_nifty_bear_bot.py")
            if success:
                ran_today["india_bear"] = today
            else:
                log.warning(f"  ⚠️ Bear bot failed to launch — will retry in {RETRY_COOLDOWN_MINUTES} min")

        # ── 10:00 AM ET — US Paper Bot ───────────────────────────────────
        if (now_et.hour == 10 and now_et.minute >= 0 and
                ran_today["us_paper"] != today and
                is_us_market_day(now_et.date())):

            log.info(f"\n{'='*50}")
            log.info(f"  10:00 AM ET — Running US paper bot")
            log.info(f"{'='*50}")
            run_script("trading_bot.py")
            ran_today["us_paper"] = today

        # ── 9:20 AM IST — Strategy Four Breakout Bot (background) ────────
        # Launched once per day in background — manages its own scan loop
        # until 3:15 PM independently of other bots
        if (STRATEGY_FOUR_ENABLED and
                now_ist.hour >= 9 and now_ist.minute >= 20 and
                now_ist.hour < 15 and
                ran_today["strategy_four"] != today and
                is_india_market_day(today)):

            log.info(f"\n{'='*50}")
            log.info(f"  Launching Strategy Four Breakout Bot (background)")
            log.info(f"{'='*50}")
            success = run_script_background("live_breakout_bot.py")
            if success:
                ran_today["strategy_four"] = today
            else:
                log.warning("  ⚠️ Strategy Four failed to launch")

        time.sleep(30)

if __name__ == "__main__":
    main()
