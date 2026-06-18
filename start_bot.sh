#!/bin/bash
# Start trading bot with auto-restart on crash
cd /root/trading_bot

while true; do
    echo "[$(date)] Starting main scheduler..."
    python3 main_scheduler.py
    echo "[$(date)] Scheduler stopped. Restarting in 10 seconds..."
    sleep 10
done
