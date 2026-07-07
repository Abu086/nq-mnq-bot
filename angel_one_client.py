"""
Angel One SmartAPI — Live Market Data Connector
=================================================
Connects to Angel One's real SmartAPI to fetch:
  - Live NIFTY / BANKNIFTY index price
  - Live option chain premiums (CE/PE) for any strike
  - Historical candles for EMA calculation

This module does NOT place real orders — it's used only to fetch
REAL market data so the paper trading bot can simulate trades
against actual live prices instead of random numbers.

Credentials are read from environment variables (set as GitHub Secrets):
  ANGEL_API_KEY
  ANGEL_SECRET_KEY
  ANGEL_CLIENT_ID
  ANGEL_PASSWORD
  ANGEL_TOTP_SECRET
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

BASE_URL = "https://apiconnect.angelone.in"

# ── TOTP Generator (no external pyotp dependency needed) ─────────────────────
def generate_totp(secret: str, digits: int = 6, interval: int = 30) -> str:
    """
    Generate a TOTP code from a base32 secret — pure Python, no pyotp needed.
    Implements RFC 6238 (same algorithm Google Authenticator uses).
    """
    secret = secret.strip().replace(" ", "").upper()
    # Pad base32 string if needed
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + padding)

    counter = int(time.time() // interval)
    counter_bytes = struct.pack(">Q", counter)

    hmac_hash = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = hmac_hash[-1] & 0x0F
    truncated = hmac_hash[offset:offset + 4]
    code_int = struct.unpack(">I", truncated)[0] & 0x7FFFFFFF
    code = str(code_int % (10 ** digits)).zfill(digits)
    return code


# ── HTTP Helper ───────────────────────────────────────────────────────────────
def _request(method: str, path: str, headers: dict, body: dict = None) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        log.error(f"HTTP {e.code} on {path}: {err_body}")
        return {"status": False, "message": err_body}
    except Exception as e:
        log.error(f"Request failed on {path}: {e}")
        return {"status": False, "message": str(e)}


class AngelOneClient:
    """Lightweight Angel One SmartAPI client — login + market data only."""

    def __init__(self):
        self.api_key      = os.environ["ANGEL_API_KEY"]
        self.client_id    = os.environ["ANGEL_CLIENT_ID"]
        self.password     = os.environ["ANGEL_PASSWORD"]
        self.totp_secret  = os.environ["ANGEL_TOTP_SECRET"]
        self.jwt_token    = None
        self.feed_token    = None
        self.refresh_token = None

    def _headers(self, auth=False) -> dict:
        h = {
            "Content-Type":     "application/json",
            "Accept":           "application/json",
            "X-UserType":       "USER",
            "X-SourceID":       "WEB",
            "X-ClientLocalIP":  "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress":     "00:00:00:00:00:00",
            "X-PrivateKey":     self.api_key,
        }
        if auth and self.jwt_token:
            h["Authorization"] = f"Bearer {self.jwt_token}"
        return h

    def login(self) -> bool:
        """Authenticate with Angel One using TOTP. Returns True on success."""
        totp = generate_totp(self.totp_secret)
        body = {
            "clientcode": self.client_id,
            "password":   self.password,
            "totp":       totp,
        }
        resp = _request("POST", "/rest/auth/angelbroking/user/v1/loginByPassword",
                        self._headers(), body)

        if not resp.get("status"):
            log.error(f"Angel One login failed: {resp.get('message')}")
            return False

        data = resp["data"]
        self.jwt_token      = data["jwtToken"]
        self.refresh_token  = data["refreshToken"]
        self.feed_token      = data.get("feedToken")
        log.info("✅ Angel One login successful")
        return True

    def get_ltp(self, exchange: str, trading_symbol: str, symbol_token: str) -> float | None:
        """Get Last Traded Price for any instrument (index, future, option)."""
        body = {
            "exchange":        exchange,        # 'NSE', 'NFO', etc.
            "tradingsymbol":   trading_symbol,
            "symboltoken":     symbol_token,
        }
        resp = _request("POST", "/rest/secure/angelbroking/order/v1/getLtpData",
                        self._headers(auth=True), body)
        if not resp.get("status"):
            log.warning(f"LTP fetch failed for {trading_symbol}: {resp.get('message')}")
            return None
        return resp["data"]["ltp"]

    def get_candle_data(self, exchange: str, symbol_token: str,
                        interval: str = "ONE_DAY", days: int = 30) -> list | None:
        """Fetch historical candle data for EMA calculation."""
        import datetime
        to_date   = datetime.datetime.now()
        from_date = to_date - datetime.timedelta(days=days)

        body = {
            "exchange":     exchange,
            "symboltoken":  symbol_token,
            "interval":     interval,
            "fromdate":     from_date.strftime("%Y-%m-%d %H:%M"),
            "todate":       to_date.strftime("%Y-%m-%d %H:%M"),
        }
        resp = _request("POST", "/rest/secure/angelbroking/historical/v1/getCandleData",
                        self._headers(auth=True), body)
        if not resp.get("status"):
            log.warning(f"Candle fetch failed: {resp.get('message')}")
            return None
        # Each candle: [timestamp, open, high, low, close, volume]
        return resp["data"]

    def search_scrip(self, exchange: str, search_text: str) -> list | None:
        """Search for a trading symbol (e.g. option contract) to get its token."""
        body = {"exchange": exchange, "searchscrip": search_text}
        resp = _request("POST", "/rest/secure/angelbroking/order/v1/searchScrip",
                        self._headers(auth=True), body)
        if not resp.get("status"):
            log.warning(f"Scrip search failed for {search_text}: {resp.get('message')}")
            return None
        return resp.get("data")


    def get_available_margin(self) -> float:
        """Fetch live available cash/margin from Angel One RMS."""
        resp = _request("GET", "/rest/secure/angelbroking/user/v1/getRMS",
                         self._headers(auth=True))
        if not resp.get("status"):
            log.warning(f"RMS fetch failed: {resp.get('message')}")
            return 0.0
        data = resp.get("data") or {}
        return float(data.get("availablecash", 0) or 0)

    def get_basket_margin(self, positions: list) -> dict | None:
        """
        Calculate the REAL required margin for a basket of positions (e.g. a
        BUY + SELL spread) BEFORE placing any order. This correctly accounts
        for hedge benefit when legs are submitted together as one basket.

        positions: list of dicts, each like:
            {
                "exchange":    "NFO",
                "qty":         65,
                "price":       0.0,
                "productType": "INTRADAY",
                "token":       "12345",
                "tradeType":   "BUY"  or  "SELL",
            }

        Returns the response 'data' dict (contains totalMarginRequired, etc.)
        or None on failure.
        """
        body = {"positions": positions}
        resp = _request("POST", "/rest/secure/angelbroking/margin/v1/batch",
                        self._headers(auth=True), body)
        if not resp.get("status"):
            log.warning(f"Margin calculation failed: {resp.get('message')}")
            return None
        return resp.get("data")


# ── Index Token Reference (well-known, stable tokens on NSE) ─────────────────
INDEX_TOKENS = {
    "NIFTY":     {"exchange": "NSE", "trading_symbol": "Nifty 50",     "token": "99926000"},
    "BANKNIFTY": {"exchange": "NSE", "trading_symbol": "Nifty Bank",   "token": "99926009"},
}


def get_live_index_price(client: AngelOneClient, inst_name: str) -> float | None:
    """Fetch live NIFTY / BANKNIFTY spot price."""
    ref = INDEX_TOKENS.get(inst_name)
    if not ref:
        return None
    return client.get_ltp(ref["exchange"], ref["trading_symbol"], ref["token"])


def get_option_ltp(client: AngelOneClient, trading_symbol: str, symbol_token: str) -> float | None:
    """Fetch live premium for a specific option contract (e.g. NIFTY16JUN2623300CE)."""
    return client.get_ltp("NFO", trading_symbol, symbol_token)
