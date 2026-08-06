"""
Strategy Eight -- Position/State Tracking
------------------------------------------------------------------
One trade per symbol per day. A symbol only becomes eligible if
yesterday's daily RSI(2) was an extreme (<=10 or >95) AND today's open
gapped 0.3-1.5% consistent with that extreme (see strategy_eight_live.py
for the full signal logic). Extra fields (prev_rsi2, gap_pct,
entry_reason) are stored per position purely for the trading journal /
post-hoc review -- they don't affect execution.
"""

import os
import json
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = "/root/trading_bot/strategy_eight/state"


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d")


def state_path(date_str: str = None) -> str:
    date_str = date_str or today_ist_str()
    return os.path.join(STATE_DIR, f"{date_str}.json")


def _empty_state(date_str: str) -> dict:
    return {
        "date": date_str,
        "positions": {},
        "traded": [],
        "last_heartbeat": None,
    }


def load_today_state() -> dict:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = state_path()
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return _empty_state(today_ist_str())


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = state_path(state["date"])
    dir_ = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".tmp_state_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def heartbeat(state: dict) -> dict:
    state["last_heartbeat"] = now_ist().isoformat()
    save_state(state)
    return state


def has_traded_today(state: dict, symbol: str) -> bool:
    return symbol in state["traded"]


def mark_entry(state: dict, symbol: str, direction: str, entry_price: float,
               qty: int, entry_order_id: str, stop: float, target: float,
               prev_rsi2: float = None, gap_pct: float = None) -> dict:
    state["positions"][symbol] = {
        "symbol": symbol,
        "direction": direction,
        "status": "open",
        "entry_price": entry_price,
        "entry_time": now_ist().strftime("%H:%M:%S"),
        "entry_order_id": entry_order_id,
        "qty": qty,
        "stop": stop,
        "target": target,
        "prev_rsi2": prev_rsi2,
        "gap_pct": gap_pct,
        "exit_price": None,
        "exit_time": None,
        "exit_order_id": None,
        "exit_reason": None,
        "pnl": None,
    }
    state["traded"].append(symbol)
    save_state(state)
    return state


def mark_exit(state: dict, symbol: str, exit_price: float,
              exit_order_id: str, exit_reason: str) -> dict:
    pos = state["positions"][symbol]
    pos["status"] = "closed"
    pos["exit_price"] = exit_price
    pos["exit_time"] = now_ist().strftime("%H:%M:%S")
    pos["exit_order_id"] = exit_order_id
    pos["exit_reason"] = exit_reason
    if pos["direction"] == "LONG":
        pos["pnl"] = round(pos["qty"] * (exit_price - pos["entry_price"]), 2)
    else:
        pos["pnl"] = round(pos["qty"] * (pos["entry_price"] - exit_price), 2)
    save_state(state)
    return state


def open_positions(state: dict) -> dict:
    return {k: v for k, v in state["positions"].items() if v["status"] == "open"}


def is_new_trading_day(state: dict) -> bool:
    return state["date"] != today_ist_str()
