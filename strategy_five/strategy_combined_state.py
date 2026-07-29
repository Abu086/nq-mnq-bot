"""
Combined Strategy (Strategy Five SHORT + Strategy 6.3 BUY) -- Position/State Tracking
------------------------------------------------------------------
Direction-aware evolution of strategy_five_state.py. A single symbol can
independently hold a SHORT position (short-sell side, Strategy Five's own
rules/blacklist) AND a BUY position (dip-buy side, Strategy 6.3's own
rules/blacklist) on the SAME day, since the two triggers are logically
independent (empirically they never overlapped in the 180-day backtest,
but nothing prevents an extreme intraday range from firing both). Every
position is therefore keyed by "SYMBOL|DIRECTION", not just SYMBOL.

All timestamps are Indian Standard Time (Asia/Kolkata), regardless of the
VPS's system timezone or where the operator is physically located. This
module never reads the system's local time zone for anything that matters
-- every "now" comes from now_ist().

State file: /root/trading_bot/strategy_five/state/<YYYY-MM-DD>.json (IST date)
Writes are atomic (write to a temp file, then os.replace) so a crash or
power-loss mid-write can never corrupt the state file.

Still Strategy Five's own directory/state file naming convention -- this
combined bot IS the replacement for the live Strategy Five deployment, in
the same folder, per the user's explicit "replace in place" instruction.
Strategy Four's files/state are never touched by this module.
"""

import os
import json
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = "/root/trading_bot/strategy_five/state"


def now_ist() -> datetime:
    """The one and only source of 'what time is it' for this bot.
    Always Indian time, never the VPS's local system clock."""
    return datetime.now(IST)


def today_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d")


def state_path(date_str: str = None) -> str:
    date_str = date_str or today_ist_str()
    return os.path.join(STATE_DIR, f"{date_str}.json")


def _key(symbol: str, direction: str) -> str:
    return f"{symbol}|{direction}"


def _empty_state(date_str: str) -> dict:
    return {
        "date": date_str,
        "positions": {},     # "SYMBOL|DIRECTION" -> position dict
        "traded": [],        # list of "SYMBOL|DIRECTION" -- one-trade-per-symbol-per-direction-per-day guard
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
    """Atomic write: temp file in the same directory, then os.replace.
    Guarantees the state file is never left half-written."""
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
    """Call periodically from the main loop so an external health check can
    detect a stalled/dead bot (staleness of this timestamp) during market
    hours."""
    state["last_heartbeat"] = now_ist().isoformat()
    save_state(state)
    return state


def has_traded_today(state: dict, symbol: str, direction: str) -> bool:
    """One trade per stock per direction per day guard. SHORT and BUY are
    tracked independently -- a symbol can trade on both sides same day."""
    return _key(symbol, direction) in state["traded"]


def mark_entry1(state: dict, symbol: str, direction: str, is_gap: bool,
                 entry1_price: float, entry1_qty: int, entry1_order_id: str,
                 risk_level) -> dict:
    """Record the first leg of a new position.

    direction: "SHORT" or "BUY".
    risk_level is None if averaging is still eligible for this entry (gap
    case, SHORT side only in this build -- BUY/6.3 never averages, its
    risk_level is always set immediately at entry1). Once/if Leg 2 fires
    (SHORT only), risk_level activates from the blended average."""
    key = _key(symbol, direction)
    state["positions"][key] = {
        "symbol": symbol,
        "direction": direction,
        "status": "open",
        "is_gap": is_gap,
        "entry1_price": entry1_price,
        "entry1_time": now_ist().strftime("%H:%M:%S"),
        "entry1_qty": entry1_qty,
        "entry1_order_id": entry1_order_id,
        "leg2_price": None,
        "leg2_time": None,
        "leg2_qty": None,
        "leg2_order_id": None,
        "avg_entry_price": entry1_price,
        "risk_level": risk_level,
        "total_qty": entry1_qty,
        "exit_price": None,
        "exit_time": None,
        "exit_order_id": None,
        "exit_reason": None,
        "pnl": None,
    }
    state["traded"].append(key)
    save_state(state)
    return state


def mark_leg2(state: dict, symbol: str, direction: str, leg2_price: float,
              leg2_qty: int, leg2_order_id: str, risk_pct: float) -> dict:
    """Record the averaging (second) leg. Recomputes the blended average
    entry price and activates the risk level from that new average.
    SHORT: risk_level = avg_entry * (1 + risk_pct/100) (stop ABOVE entry).
    BUY:   risk_level = avg_entry * (1 - risk_pct/100) (stop BELOW entry).
    (Only SHORT uses this in the current live build -- BUY/6.3 runs the
    No-Averaging variant -- but this is implemented direction-correctly
    for consistency/future use.)"""
    key = _key(symbol, direction)
    pos = state["positions"][key]
    qty1 = pos["entry1_qty"]
    entry1 = pos["entry1_price"]
    total_qty = qty1 + leg2_qty
    avg_entry = ((qty1 * entry1) + (leg2_qty * leg2_price)) / total_qty if total_qty else entry1

    pos["leg2_price"] = leg2_price
    pos["leg2_time"] = now_ist().strftime("%H:%M:%S")
    pos["leg2_qty"] = leg2_qty
    pos["leg2_order_id"] = leg2_order_id
    pos["avg_entry_price"] = avg_entry
    pos["total_qty"] = total_qty
    if direction == "SHORT":
        pos["risk_level"] = avg_entry * (1 + risk_pct / 100)
    else:
        pos["risk_level"] = avg_entry * (1 - risk_pct / 100)
    save_state(state)
    return state


def mark_exit(state: dict, symbol: str, direction: str, exit_price: float,
              exit_order_id: str, exit_reason: str) -> dict:
    """Direction-aware P&L:
    SHORT: sold high, buys back to cover -> pnl = qty * (avg_entry - exit).
    BUY:   bought low, sells to close    -> pnl = qty * (exit - avg_entry)."""
    key = _key(symbol, direction)
    pos = state["positions"][key]
    pos["status"] = "closed"
    pos["exit_price"] = exit_price
    pos["exit_time"] = now_ist().strftime("%H:%M:%S")
    pos["exit_order_id"] = exit_order_id
    pos["exit_reason"] = exit_reason
    if direction == "SHORT":
        pos["pnl"] = round(pos["total_qty"] * (pos["avg_entry_price"] - exit_price), 2)
    else:
        pos["pnl"] = round(pos["total_qty"] * (exit_price - pos["avg_entry_price"]), 2)
    save_state(state)
    return state


def open_positions(state: dict, direction: str = None) -> dict:
    """Keys are 'SYMBOL|DIRECTION'. Pass direction="SHORT"/"BUY" to filter,
    or None for both sides -- what the monitoring loop needs to watch for
    averaging triggers / risk level / EOD square-off."""
    out = {}
    for key, pos in state["positions"].items():
        if pos["status"] != "open":
            continue
        if direction is not None and pos["direction"] != direction:
            continue
        out[key] = pos
    return out


def is_new_trading_day(state: dict) -> bool:
    """True if the loaded state is stale (from a previous day) and a fresh
    state should be started -- guards against a restart on day N+1 picking
    up day N's traded/positions by mistake."""
    return state["date"] != today_ist_str()
